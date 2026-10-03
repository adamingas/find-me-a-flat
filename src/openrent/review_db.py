"""Normalized review state and the archived evidence behind each judgement."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from email.headerregistry import HeaderRegistry
from typing import Any

from .async_db import AsyncDatabase
from .db import Database, _now
from .email_sender import SENDER_EMAIL

_OBSERVATION_COLUMNS = {"first_seen_at", "last_seen_at", "updated_at"}


@dataclass(frozen=True)
class ArchivedImage:
    sha256: str
    content_type: str
    content: bytes
    caption: str | None
    source_url: str
    kind: str
    position: int


@dataclass(frozen=True)
class ReviewSnapshot:
    property_id: int
    fingerprint: str
    data: dict[str, Any]
    images: tuple[ArchivedImage, ...]


def _field(value: Any, name: str) -> Any:
    return value[name] if isinstance(value, Mapping) else getattr(value, name)


def _recipient_identity(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("Email recipient must be one valid mailbox")
    header = HeaderRegistry()("To", value)
    if header.defects or len(header.addresses) != 1:
        raise ValueError("Email recipient must be one valid mailbox")
    address = header.addresses[0]
    if not address.username or not address.domain:
        raise ValueError("Email recipient must be one valid mailbox")
    return address.addr_spec.casefold()


class ReviewDatabase(Database):
    """Review each property once, independently of repeated archive observations.

    The caller holds an OS review lock for the complete run. A transaction still
    guards snapshot claims/completion against a concurrent property importer.
    Incomplete or changed inputs never become processed reviews. A completed
    review remains processed when listing facts or review criteria later change.
    """

    @contextmanager
    def _transaction(self, *, write: bool = True):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield

    def register_profile(
        self, profile_key: str, criteria: str, model: str, backend: str = "codex"
    ) -> None:
        with self.connection:
            previous = self.connection.execute(
                "SELECT criteria, model, backend FROM review.review_profiles WHERE profile_key = ?",
                (profile_key,),
            ).fetchone()
            if previous is not None:
                if (previous["criteria"], previous["model"], previous["backend"]) != (
                    criteria,
                    model,
                    backend,
                ):
                    raise ValueError(
                        "Review profile key already belongs to different criteria/model/backend"
                    )
                return
            self.connection.execute(
                "INSERT INTO review.review_profiles (profile_key, criteria, model, backend, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (profile_key, criteria, model, backend, _now()),
            )

    def _snapshot(self, property_id: int, *, load_images: bool = True) -> ReviewSnapshot | None:
        row = self.get_property(property_id)
        if row is None or row["is_live"] == 0:
            return None
        data = dict(row)
        data["extra_metadata"] = json.loads(data.pop("extra_metadata_json"))
        for name, ordering in (
            ("property_features", "section, feature_key"),
            ("nearby_places", "kind, name"),
            ("media_links", "kind, url"),
        ):
            data[name.removeprefix("property_")] = [
                dict(item)
                for item in self.connection.execute(
                    f"SELECT * FROM {name} WHERE property_id = ? ORDER BY {ordering}",
                    (property_id,),
                )
            ]
        columns = ", b.content" if load_images else ""
        gallery = self.connection.execute(
            "SELECT i.*, b.content_type, b.byte_length" + columns + " FROM property_images i "
            "LEFT JOIN image_blobs b ON b.sha256 = i.content_sha256 "
            "WHERE i.property_id = ? ORDER BY i.position, i.source_url",
            (property_id,),
        ).fetchall()
        if not any(image["kind"] == "photo" for image in gallery) or any(
            image["download_status"] != "downloaded" or image["byte_length"] is None
            for image in gallery
        ):
            return None
        data["images"] = [
            {name: image[name] for name in dict(image) if name != "content"} for image in gallery
        ]
        # The model gets the entire archive row and related metadata. Observation
        # timestamps and HTML backups do not change the substantive evidence
        # fingerprint when a scanner refreshes the same listing concurrently.
        fingerprint_data = {
            name: value
            for name, value in data.items()
            if name not in _OBSERVATION_COLUMNS | {"source_html", "description_html"}
        }
        for name in ("features", "nearby_places", "media_links", "images"):
            fingerprint_data[name] = [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    not in _OBSERVATION_COLUMNS
                    | {"downloaded_at", "etag", "last_modified", "last_error"}
                }
                for item in data[name]
            ]
        canonical = json.dumps(
            fingerprint_data,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        images = (
            tuple(
                ArchivedImage(
                    sha256=image["content_sha256"],
                    content_type=image["content_type"],
                    content=image["content"],
                    caption=image["caption"],
                    source_url=image["source_url"],
                    kind=image["kind"],
                    position=image["position"],
                )
                for image in gallery
            )
            if load_images
            else ()
        )
        return ReviewSnapshot(property_id, fingerprint, data, images)

    def _already_processed(self, property_id: int) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM review.property_reviews WHERE property_id = ? AND status = 'complete' LIMIT 1",
                (property_id,),
            ).fetchone()
            is not None
        )

    def unprocessed_ids(self, limit: int | None = None) -> list[int]:
        """Return live IDs with no completed review, including incomplete galleries."""
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise ValueError("Review limit must be a positive integer")
        query = (
            "SELECT p.id FROM main.properties p "
            "WHERE (p.is_live IS NULL OR p.is_live != 0) "
            "AND NOT EXISTS (SELECT 1 FROM review.property_reviews r "
            "WHERE r.property_id = p.id AND r.status = 'complete') ORDER BY p.id"
        )
        parameters = () if limit is None else (limit,)
        if limit is not None:
            query += " LIMIT ?"
        with self._transaction(write=False):
            return [row[0] for row in self.connection.execute(query, parameters)]

    def pending_review_images(self, property_id: int) -> list[dict[str, Any]]:
        """Return image rows requiring download, including broken BLOB references."""
        with self._transaction(write=False):
            return [
                dict(row)
                for row in self.connection.execute(
                    "SELECT i.* FROM main.property_images i "
                    "LEFT JOIN main.image_blobs b ON b.sha256 = i.content_sha256 "
                    "WHERE i.property_id = ? "
                    "AND (i.download_status != 'downloaded' OR b.sha256 IS NULL) "
                    "ORDER BY i.position, i.source_url",
                    (property_id,),
                )
            ]

    def candidate_ids(
        self,
        profile_key: str,
        once_per_property: bool = True,
        limit: int | None = None,
    ) -> list[int]:
        if limit is not None and limit < 1:
            raise ValueError("Review limit must be positive")
        with self._transaction(write=False):
            ids = [
                row[0] for row in self.connection.execute("SELECT id FROM properties ORDER BY id")
            ]
            candidates = []
            for property_id in ids:
                if once_per_property and self._already_processed(property_id):
                    continue
                snapshot = self._snapshot(property_id, load_images=False)
                if snapshot is None:
                    continue
                if (
                    self.connection.execute(
                        "SELECT 1 FROM review.property_reviews WHERE profile_key = ? AND property_id = ? "
                        "AND fingerprint = ? AND status = 'complete'",
                        (profile_key, property_id, snapshot.fingerprint),
                    ).fetchone()
                    is None
                ):
                    candidates.append(property_id)
                    if limit is not None and len(candidates) == limit:
                        break
            return candidates

    def claim_review(
        self,
        profile_key: str,
        property_id: int,
        once_per_property: bool = True,
    ) -> tuple[int, ReviewSnapshot] | None:
        with self._transaction():
            if once_per_property and self._already_processed(property_id):
                return None
            snapshot = self._snapshot(property_id)
            if snapshot is None:
                return None
            previous = self.connection.execute(
                "SELECT id, status FROM review.property_reviews "
                "WHERE profile_key = ? AND property_id = ? AND fingerprint = ?",
                (profile_key, property_id, snapshot.fingerprint),
            ).fetchone()
            if previous is not None and previous["status"] == "complete":
                return None
            values = (
                _now(),
                snapshot.data["title"],
                snapshot.data["url"],
                snapshot.data["address_display"],
                snapshot.data["rent_pcm_pence"],
                snapshot.data["bedrooms"],
            )
            if previous is None:
                cursor = self.connection.execute(
                    "INSERT INTO review.property_reviews "
                    "(profile_key, property_id, fingerprint, started_at, reviewed_title, "
                    "reviewed_url, reviewed_address_display, reviewed_rent_pcm_pence, "
                    "reviewed_bedrooms, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing')",
                    (profile_key, property_id, snapshot.fingerprint, *values),
                )
                review_id = cursor.lastrowid
            else:
                review_id = previous["id"]
                self.connection.execute(
                    "UPDATE review.property_reviews SET started_at = ?, reviewed_title = ?, "
                    "reviewed_url = ?, reviewed_address_display = ?, reviewed_rent_pcm_pence = ?, "
                    "reviewed_bedrooms = ?, status = 'processing', result = NULL, "
                    "error = NULL, processed_at = NULL WHERE id = ?",
                    (*values, review_id),
                )
            self.connection.execute(
                "DELETE FROM review.review_images WHERE review_id = ?", (review_id,)
            )
            self.connection.executemany(
                "INSERT INTO review.review_images "
                "(review_id, position, source_position, sha256, source_url, kind, caption) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        review_id,
                        index,
                        image.position,
                        image.sha256,
                        image.source_url,
                        image.kind,
                        image.caption,
                    )
                    for index, image in enumerate(snapshot.images)
                ],
            )
            assert review_id is not None
            return review_id, snapshot

    def complete_review(self, review_id: int, judgement: Any) -> bool:
        decision, summary = _field(judgement, "decision"), _field(judgement, "summary")
        result = _field(judgement, "result")
        if decision not in {"pass", "reject", "uncertain"} or not isinstance(summary, str):
            raise ValueError("Invalid review judgement")
        if not isinstance(result, dict):
            raise TypeError("Review result must be a structured object")
        if result.get("decision") != decision or result.get("summary") != summary:
            raise ValueError("Review result does not match its decision and summary")
        try:
            serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("Review result must be serializable with finite values") from exc
        with self._transaction():
            review = self.connection.execute(
                "SELECT * FROM review.property_reviews WHERE id = ?", (review_id,)
            ).fetchone()
            if review is None or review["status"] != "processing":
                raise ValueError("Review is not currently processing")
            snapshot = self._snapshot(review["property_id"], load_images=False)
            if snapshot is None or snapshot.fingerprint != review["fingerprint"]:
                self.connection.execute(
                    "UPDATE review.property_reviews SET status = 'error', error = ?, processed_at = NULL "
                    "WHERE id = ?",
                    (
                        "Listing facts or gallery changed during review",
                        review_id,
                    ),
                )
                return False
            self.connection.execute(
                "UPDATE review.property_reviews SET status = 'complete', result = jsonb(?), "
                "processed_at = ?, error = NULL WHERE id = ?",
                (serialized, _now(), review_id),
            )
            return True

    def fail_review(self, review_id: int, error: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE review.property_reviews SET status = 'error', error = ?, processed_at = NULL "
                "WHERE id = ? AND status != 'complete'",
                (error, review_id),
            )

    def _email_review(self, review_id: int) -> Any:
        return self.connection.execute(
            "SELECT r.*, json(r.result) AS result_text FROM review.property_reviews r "
            "JOIN main.properties p ON p.id = r.property_id "
            "WHERE r.id = ? AND r.status = 'complete' AND r.decision IN ('pass', 'uncertain') "
            "AND (p.is_live IS NULL OR p.is_live != 0) "
            "AND r.id = (SELECT latest.id FROM review.property_reviews latest "
            "WHERE latest.property_id = r.property_id AND latest.status = 'complete' "
            "ORDER BY latest.processed_at DESC, latest.id DESC LIMIT 1)",
            (review_id,),
        ).fetchone()

    def pending_notifications(self, recipient: str) -> list[dict[str, Any]]:
        """Return unreserved passes and uncertain reviews with their judged facts and gallery."""
        recipient = _recipient_identity(recipient)
        with self._transaction(write=False):
            review_ids = [
                row[0]
                for row in self.connection.execute(
                    "SELECT r.id FROM review.property_reviews r "
                    "WHERE r.status = 'complete' AND r.decision IN ('pass', 'uncertain') "
                    "AND NOT EXISTS (SELECT 1 FROM review.email_batch_items i "
                    "WHERE i.recipient = ? AND i.property_id = r.property_id) "
                    "ORDER BY r.property_id, r.id",
                    (recipient,),
                )
            ]
            records = []
            for review_id in review_ids:
                reviewed = self._email_review(review_id)
                if reviewed is None:
                    continue
                property_data = dict(self.get_property(reviewed["property_id"]))
                property_data["extra_metadata"] = json.loads(
                    property_data.pop("extra_metadata_json")
                )
                for table, name, ordering in (
                    ("property_features", "features", "section, feature_key"),
                    ("nearby_places", "nearby_places", "kind, name"),
                    ("media_links", "media_links", "kind, url"),
                ):
                    property_data[name] = [
                        dict(row)
                        for row in self.connection.execute(
                            f"SELECT * FROM main.{table} WHERE property_id = ? ORDER BY {ordering}",
                            (reviewed["property_id"],),
                        )
                    ]
                property_data["images"] = [
                    dict(row)
                    for row in self.connection.execute(
                        "SELECT source_url, position, kind, caption FROM review.review_images "
                        "WHERE review_id = ? ORDER BY position",
                        (review_id,),
                    )
                ]
                for name in ("title", "url", "address_display", "rent_pcm_pence", "bedrooms"):
                    # An unknown judged fact stays unknown; it must not silently
                    # inherit a later value that the model never assessed.
                    property_data[name] = reviewed["reviewed_" + name]
                records.append(
                    {
                        "review_id": review_id,
                        "property_id": reviewed["property_id"],
                        **{
                            name: property_data[name]
                            for name in (
                                "title",
                                "url",
                                "address_display",
                                "rent_pcm_pence",
                                "bedrooms",
                            )
                        },
                        "property": property_data,
                        "result": json.loads(reviewed["result_text"]),
                    }
                )
            return records

    def pending_email_batch(self, recipient: str) -> dict[str, Any] | None:
        """Return unresolved state; only pending/failed bodies can safely be retried.

        The caller's run lock means a leftover sending row belongs to an
        interrupted attempt. The provider may have accepted it, so quarantine it
        instead of automatically sending it again.
        """
        recipient = _recipient_identity(recipient)
        with self._transaction():
            self.connection.execute(
                "UPDATE review.email_batches SET status = 'unknown', error = ? "
                "WHERE recipient = ? AND status = 'sending'",
                (
                    "Previous sender stopped before confirming provider acceptance; reconcile manually",
                    recipient,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM review.email_batches WHERE recipient = ? "
                "AND status IN ('pending', 'failed', 'unknown') "
                "ORDER BY (status = 'unknown') DESC, id LIMIT 1",
                (recipient,),
            ).fetchone()
            if row is None:
                return None
            batch = dict(row)
            if batch["status"] in {"pending", "failed"} and batch["sender"] != SENDER_EMAIL:
                self.connection.execute(
                    "UPDATE review.email_batches SET sender = ? WHERE id = ?",
                    (SENDER_EMAIL, batch["id"]),
                )
                batch["sender"] = SENDER_EMAIL
            return batch

    def create_email_batch(
        self, recipient: str, review_ids: list[int], subject: str, html_body: str, text_body: str
    ) -> dict[str, Any]:
        recipient = _recipient_identity(recipient)
        if not review_ids or len(set(review_ids)) != len(review_ids):
            raise ValueError("An email batch requires distinct notifiable reviews")
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in subject)
        ):
            raise ValueError("Email subject must be a nonempty single line")
        if not isinstance(html_body, str) or not isinstance(text_body, str):
            raise TypeError("Email batch bodies must be strings")
        with self._transaction():
            property_ids = []
            for review_id in review_ids:
                reviewed = self._email_review(review_id)
                if reviewed is None:
                    raise ValueError("An email batch contains an ineligible review")
                property_ids.append(reviewed["property_id"])
            if len(set(property_ids)) != len(property_ids):
                raise ValueError("An email batch requires distinct property IDs")
            cursor = self.connection.execute(
                "INSERT INTO review.email_batches "
                "(recipient, sender, subject, html_body, text_body, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (recipient, SENDER_EMAIL, subject, html_body, text_body, _now()),
            )
            self.connection.executemany(
                "INSERT INTO review.email_batch_items (batch_id, recipient, property_id, review_id) "
                "VALUES (?, ?, ?, ?)",
                [
                    (cursor.lastrowid, recipient, property_id, review_id)
                    for property_id, review_id in zip(property_ids, review_ids, strict=True)
                ],
            )
            return dict(
                self.connection.execute(
                    "SELECT * FROM review.email_batches WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
            )

    def mark_email_sending(self, batch_id: int) -> bool:
        with self.connection:
            return (
                self.connection.execute(
                    "UPDATE review.email_batches SET status = 'sending', last_attempt_at = ?, "
                    "attempts = attempts + 1, error = NULL WHERE id = ? AND status IN ('pending', 'failed')",
                    (_now(), batch_id),
                ).rowcount
                == 1
            )

    def finish_email_batch(self, batch_id: int, provider_message_id: str | None) -> None:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE review.email_batches SET status = 'sent', sent_at = ?, "
                "provider_message_id = ?, error = NULL WHERE id = ? AND status = 'sending'",
                (_now(), provider_message_id, batch_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only a sending email batch can be confirmed sent")

    def fail_email_batch(self, batch_id: int, error: str, indeterminate: bool = False) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE review.email_batches SET status = ?, error = ? "
                "WHERE id = ? AND status IN ('pending', 'sending', 'failed')",
                ("unknown" if indeterminate else "failed", error, batch_id),
            )

    def counts(self) -> dict[str, int]:
        result = super().counts()
        for table in (
            "review_profiles",
            "property_reviews",
            "review_images",
            "email_batches",
            "email_batch_items",
        ):
            result[table] = self.connection.execute(
                f"SELECT count(*) FROM review.{table}"
            ).fetchone()[0]
        for status in ("processing", "complete", "error"):
            result[f"reviews_{status}"] = self.connection.execute(
                "SELECT count(*) FROM review.property_reviews WHERE status = ?",
                (status,),
            ).fetchone()[0]
        for status in ("pending", "sending", "sent", "failed", "unknown"):
            result[f"emails_{status}"] = self.connection.execute(
                "SELECT count(*) FROM review.email_batches WHERE status = ?",
                (status,),
            ).fetchone()[0]
        return result


class AsyncReviewDatabase(AsyncDatabase):
    """Use the same dedicated SQLite worker and cancellation-safe cleanup as imports."""

    def _open(self) -> None:
        self._database = ReviewDatabase(self._path)

    async def register_profile(
        self, profile_key: str, criteria: str, model: str, backend: str = "codex"
    ) -> None:
        await self._call("register_profile", profile_key, criteria, model, backend)

    async def candidate_ids(
        self,
        profile_key: str,
        once_per_property: bool = True,
        limit: int | None = None,
    ) -> list[int]:
        return await self._call("candidate_ids", profile_key, once_per_property, limit)

    async def unprocessed_ids(self, limit: int | None = None) -> list[int]:
        return await self._call("unprocessed_ids", limit)

    async def pending_review_images(self, property_id: int) -> list[dict[str, Any]]:
        return await self._call("pending_review_images", property_id)

    async def claim_review(
        self,
        profile_key: str,
        property_id: int,
        once_per_property: bool = True,
    ) -> tuple[int, ReviewSnapshot] | None:
        return await self._call("claim_review", profile_key, property_id, once_per_property)

    async def complete_review(self, review_id: int, judgement: Any) -> bool:
        return await self._call("complete_review", review_id, judgement)

    async def fail_review(self, review_id: int, error: str) -> None:
        await self._call("fail_review", review_id, error)

    async def pending_notifications(self, recipient: str) -> list[dict[str, Any]]:
        return await self._call("pending_notifications", recipient)

    async def pending_email_batch(self, recipient: str) -> dict[str, Any] | None:
        return await self._call("pending_email_batch", recipient)

    async def create_email_batch(
        self, recipient: str, review_ids: list[int], subject: str, html_body: str, text_body: str
    ) -> dict[str, Any]:
        return await self._call(
            "create_email_batch", recipient, review_ids, subject, html_body, text_body
        )

    async def mark_email_sending(self, batch_id: int) -> bool:
        return await self._call("mark_email_sending", batch_id)

    async def finish_email_batch(self, batch_id: int, provider_message_id: str | None) -> None:
        await self._call("finish_email_batch", batch_id, provider_message_id)

    async def fail_email_batch(
        self, batch_id: int, error: str, indeterminate: bool = False
    ) -> None:
        await self._call("fail_email_batch", batch_id, error, indeterminate)
