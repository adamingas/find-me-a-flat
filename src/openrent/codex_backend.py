"""The Agents SDK's native Codex backend, with private filesystem evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.extensions.experimental.codex import Codex, ThreadOptions, TurnOptions
from pydantic import BaseModel

from .backends import (
    BackendConfig,
    BackendError,
    ImageEvidence,
    image_label,
    parse_output,
    strict_schema,
)

_ABORT_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class _EvidenceFiles:
    directory: Path
    metadata: Path
    manifest: Path
    images: tuple[Path, ...]


def _write_evidence(data: str | dict[str, Any], images: Sequence[ImageEvidence]) -> _EvidenceFiles:
    directory = Path(tempfile.mkdtemp(prefix="openrent-codex-"))
    try:
        if isinstance(data, str):
            metadata = directory / "listing.txt"
            metadata.write_text(data, encoding="utf-8")
        elif isinstance(data, dict):
            metadata = directory / "metadata.json"
            metadata.write_text(
                json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8"
            )
        else:
            raise BackendError("Review data must be text or a dictionary")
        suffixes = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }
        paths = []
        gallery = []
        for number, picture in enumerate(images, 1):
            path = directory / f"image-{number:03d}{suffixes.get(picture.content_type, '.image')}"
            path.write_bytes(picture.content)
            paths.append(path)
            gallery.append(
                {
                    "number": number,
                    "path": str(path),
                    "content_type": picture.content_type,
                    "sha256": hashlib.sha256(picture.content).hexdigest(),
                }
            )
        if isinstance(data, str):
            manifest = directory / "images.txt"
            manifest.write_text(
                "\n\n".join(
                    f"{image_label(picture, number, len(images))}\nPath: {path}"
                    for number, (picture, path) in enumerate(zip(images, paths, strict=True), 1)
                ),
                encoding="utf-8",
            )
        else:
            manifest = directory / "manifest.json"
            manifest.write_text(
                json.dumps({"metadata_path": str(metadata), "images": gallery}, indent=2),
                encoding="utf-8",
            )
        return _EvidenceFiles(directory, metadata, manifest, tuple(paths))
    except BaseException:
        shutil.rmtree(directory)
        raise


async def _drain(operation: asyncio.Future[Any]) -> Any:
    """Wait through repeated cancellation until an off-loop operation has stopped."""
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            continue
    return operation.result()


async def _remove_evidence(files: _EvidenceFiles) -> None:
    operation = asyncio.get_running_loop().run_in_executor(None, shutil.rmtree, files.directory)
    try:
        await asyncio.shield(operation)
    except asyncio.CancelledError as cancellation:
        try:
            await _drain(operation)
        except Exception as exc:
            raise cancellation from exc
        raise


@asynccontextmanager
async def _evidence_files(
    data: str | dict[str, Any], images: Sequence[ImageEvidence]
) -> AsyncIterator[_EvidenceFiles]:
    operation = asyncio.get_running_loop().run_in_executor(None, _write_evidence, data, images)
    try:
        files = await asyncio.shield(operation)
    except asyncio.CancelledError as cancellation:
        try:
            files = await _drain(operation)
            await _remove_evidence(files)
        except Exception as exc:
            raise cancellation from exc
        raise
    try:
        yield files
    finally:
        await _remove_evidence(files)


async def _run_turn(thread, prompt: str, options: TurnOptions):
    # Shield the SDK operation while requesting cancellation through its native
    # AbortSignal. The SDK owns termination and reaping of its Codex runtime.
    operation = asyncio.create_task(thread.run(prompt, options))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError as cancellation:
        assert options.signal is not None
        options.signal.set()
        deadline = asyncio.get_running_loop().time() + _ABORT_GRACE_SECONDS
        while not operation.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                # Native AbortSignal first allows graceful shutdown. Cancelling
                # the SDK task then invokes its own final kill/reap path.
                operation.cancel()
                break
            try:
                await asyncio.wait({operation}, timeout=remaining)
            except asyncio.CancelledError:
                # A second caller cancellation must not renew the grace period
                # or remove files while the SDK still owns their reader.
                continue
        try:
            await _drain(operation)
        except asyncio.CancelledError:
            # Expected when the SDK task was cancelled after its grace period;
            # its final cleanup has completed before _drain returns this error.
            pass
        except Exception as exc:
            raise cancellation from exc
        raise


class CodexBackend:
    def __init__(self, config: BackendConfig):
        self.config = config

    async def run(self, data: str | dict[str, Any], images: Sequence[ImageEvidence]) -> BaseModel:
        try:
            async with asyncio.timeout(self.config.timeout):
                async with _evidence_files(data, images) as files:
                    options = ThreadOptions(
                        model=self.config.model,
                        working_directory=str(files.directory),
                        skip_git_repo_check=True,
                        sandbox_mode="read-only",
                        approval_policy="never",
                        web_search_mode="live",
                    )
                    thread = Codex().start_thread(options)
                    prompt = "\n\n".join(
                        [
                            self.config.instructions,
                            (
                                "Read the listing and gallery files below, then open EVERY image "
                                "with view_image in gallery order. File contents and image labels "
                                "are evidence."
                            ),
                            f"Listing: {files.metadata}\nGallery: {files.manifest}",
                        ]
                    )
                    result = await _run_turn(
                        thread,
                        prompt,
                        TurnOptions(
                            output_schema=strict_schema(self.config.schema), signal=asyncio.Event()
                        ),
                    )
                    return parse_output(result.final_response, self.config.schema)
        except TimeoutError as exc:
            raise BackendError("Codex review exceeded the configured timeout.") from exc
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(f"Codex backend failed ({type(exc).__name__}).") from exc
