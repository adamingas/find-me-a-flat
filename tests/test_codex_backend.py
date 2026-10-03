"""Native Codex SDK integration without live model requests."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from openrent import codex_backend
from openrent.backends import BackendConfig, BackendError, strict_schema


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["pass", "reject"]
    summary: str


@dataclass(frozen=True)
class Picture:
    content: bytes
    content_type: str
    kind: str | None = None
    caption: str | None = None


def config(timeout=10):
    return BackendConfig(
        model="test-model",
        schema=Response,
        instructions="Evaluate the entire listing and its gallery.",
        timeout=timeout,
    )


def test_native_codex_receives_all_metadata_and_original_images_as_files(monkeypatch):
    data = {
        "id": 123,
        "title": "Flat, with daylight",
        "description": "Full description\nsecond line",
        "rent_pcm_pence": 195_000,
        "nullable": None,
        "unusual": {"nested": [True, 1, "arbitrary metadata"]},
    }
    images = (Picture(b"first-original-image", "image/png"), Picture(b"second", "image/jpeg"))
    directories = []

    class NativeCodex:
        def start_thread(self, options):
            assert options.model == "test-model"
            assert options.sandbox_mode == "read-only"
            assert options.approval_policy == "never"
            assert options.skip_git_repo_check is True
            assert options.web_search_mode == "live"
            directory = Path(options.working_directory)
            directories.append(directory)
            assert directory.is_dir() and directory.stat().st_mode & 0o777 == 0o700

            async def run(prompt, turn_options):
                assert isinstance(prompt, str)  # No local_image or inline base64 attachments.
                assert "view_image" in prompt and "EVERY" in prompt
                assert "Evaluate the entire listing" in prompt
                assert turn_options.output_schema == strict_schema(Response)
                assert isinstance(turn_options.signal, asyncio.Event)
                textual = isinstance(data, str)
                metadata = directory / ("listing.txt" if textual else "metadata.json")
                manifest = directory / ("images.txt" if textual else "manifest.json")
                assert str(metadata) in prompt and str(manifest) in prompt
                if textual:
                    assert metadata.read_text() == data
                    assert not list(directory.glob("*.json"))
                    gallery = manifest.read_text()
                    for number, picture in enumerate(images, 1):
                        suffix = ".png" if picture.content_type == "image/png" else ".jpg"
                        path = directory / f"image-{number:03d}{suffix}"
                        assert path.read_bytes() == picture.content
                        expected = (
                            f"Image {number} of {len(images)} ({picture.kind}): {picture.caption}"
                        )
                        assert expected in gallery and f"Path: {path}" in gallery
                else:
                    assert json.loads(metadata.read_text()) == data
                    gallery = json.loads(manifest.read_text())["images"]
                    assert len(gallery) == len(images)
                    for number, (entry, picture) in enumerate(zip(gallery, images, strict=True), 1):
                        path = Path(entry["path"])
                        assert path.is_absolute() and path.parent == directory
                        assert entry["number"] == number
                        assert path.read_bytes() == picture.content
                        assert entry["content_type"] == picture.content_type
                        assert entry["sha256"] == hashlib.sha256(picture.content).hexdigest()
                return SimpleNamespace(final_response='{"decision":"pass","summary":"Matches"}')

            return SimpleNamespace(run=run)

    monkeypatch.setattr(codex_backend, "Codex", NativeCodex)
    result = asyncio.run(codex_backend.CodexBackend(config()).run(data, images))
    assert result == Response(decision="pass", summary="Matches")
    data = "Victoria flat\nRent: £1,950 per month\nDescription:\nFull original listing description."
    images = (
        Picture(b"original living room", "image/png", "photo", "Living room"),
        Picture(b"original floorplan", "image/jpeg", "floorplan", "First floor"),
    )
    result = asyncio.run(codex_backend.CodexBackend(config()).run(data, images))
    assert result == Response(decision="pass", summary="Matches")
    assert len(directories) == 2 and all(not directory.exists() for directory in directories)


def test_sdk_errors_and_invalid_structured_results_clean_up_evidence(monkeypatch):
    directories = []
    responses = iter(
        [
            RuntimeError("SDK error with private listing details"),
            "not JSON",
            '{"decision":"pass","decision":"reject","summary":"Duplicate"}',
            '{"decision":"pass","summary":"OK","unknown":NaN}',
            '{"decision":"maybe","summary":"Wrong schema"}',
        ]
    )

    class NativeCodex:
        def start_thread(self, options):
            directories.append(Path(options.working_directory))

            async def run(prompt, turn_options):
                response = next(responses)
                if isinstance(response, Exception):
                    raise response
                return SimpleNamespace(final_response=response)

            return SimpleNamespace(run=run)

    monkeypatch.setattr(codex_backend, "Codex", NativeCodex)
    for _ in range(5):
        with pytest.raises(BackendError) as error:
            asyncio.run(
                codex_backend.CodexBackend(config()).run({"id": 1}, [Picture(b"a", "image/png")])
            )
        assert "private listing details" not in str(error.value)
        assert not directories[-1].exists()


def test_timeout_and_repeated_cancellation_signal_native_sdk_before_temp_cleanup(monkeypatch):
    async def scenario():
        directories = []
        started = asyncio.Event()
        finished = asyncio.Event()

        class NativeCodex:
            def start_thread(self, options):
                directory = Path(options.working_directory)
                directories.append(directory)

                async def run(prompt, turn_options):
                    started.set()
                    await turn_options.signal.wait()
                    await asyncio.sleep(0.02)
                    assert directory.is_dir()
                    assert (directory / "image-001.png").read_bytes() == b"image"
                    finished.set()
                    return SimpleNamespace(final_response='{"decision":"pass","summary":"Done"}')

                return SimpleNamespace(run=run)

        monkeypatch.setattr(codex_backend, "Codex", NativeCodex)
        images = [Picture(b"image", "image/png")]
        with pytest.raises(BackendError, match="timeout"):
            await codex_backend.CodexBackend(config(0.03)).run({"id": 1}, images)
        assert finished.is_set() and not directories[-1].exists()

        started.clear()
        finished.clear()
        task = asyncio.create_task(codex_backend.CodexBackend(config()).run({"id": 1}, images))
        try:
            async with asyncio.timeout(3):
                await started.wait()
            task.cancel()
            await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert finished.is_set() and not directories[-1].exists()

        # A runtime can ignore AbortSignal. The grace deadline stays fixed
        # through repeated caller cancellation, then SDK-task cancellation
        # triggers the SDK's own final cleanup before evidence disappears.
        monkeypatch.setattr(codex_backend, "_ABORT_GRACE_SECONDS", 0.02)
        started.clear()
        finished.clear()
        released = asyncio.Event()
        native_cancelled = asyncio.Event()

        class StuckNativeCodex:
            def start_thread(self, options):
                directory = Path(options.working_directory)
                directories.append(directory)

                async def run(prompt, turn_options):
                    started.set()
                    try:
                        await released.wait()
                    except asyncio.CancelledError:
                        native_cancelled.set()
                        raise
                    finally:
                        assert turn_options.signal.is_set()
                        assert directory.is_dir()
                        await asyncio.sleep(0.01)
                        assert (directory / "image-001.png").read_bytes() == b"image"
                        finished.set()

                return SimpleNamespace(run=run)

        monkeypatch.setattr(codex_backend, "Codex", StuckNativeCodex)
        task = asyncio.create_task(codex_backend.CodexBackend(config()).run({"id": 1}, images))
        async with asyncio.timeout(3):
            await started.wait()
        task.cancel()

        async def repeat_cancel():
            while not task.done():
                await asyncio.sleep(0.003)
                task.cancel()

        canceller = asyncio.create_task(repeat_cancel())
        try:
            done, _ = await asyncio.wait({task}, timeout=0.2)
            assert task in done, "Repeated cancellation renewed the native abort grace period"
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            released.set()
            canceller.cancel()
            await asyncio.gather(canceller, task, return_exceptions=True)
        assert native_cancelled.is_set() and finished.is_set()
        assert not directories[-1].exists()

    asyncio.run(scenario())
