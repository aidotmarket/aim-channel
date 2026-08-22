"""Single listing-to-artifact resolver shared by fulfillment and verification.

Raw locators are deliberately confined to this module and its direct local callers.
They must never be serialized, logged, or included in a marketplace signature.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional

from sqlmodel import select

from app.config import settings
from app.core.database import get_session_context
from app.models.dataset import DatasetRecord
from app.models.s3_connection import S3Connection
from app.models.s3_object_metadata import S3ObjectMetadata


class ArtifactResolutionError(RuntimeError):
    """Fixed-message resolution failure safe for local logs."""


class StaleArtifactIdentityError(ArtifactResolutionError):
    """The pinned artifact is no longer the artifact selected by the handle."""


@dataclass(frozen=True)
class ResolvedArtifact:
    listing_id: str
    source_handle_id: str
    dataset: DatasetRecord
    kind: Literal["local", "s3"]
    local_path: Optional[str] = None
    connection: Optional[S3Connection] = None
    metadata: Optional[S3ObjectMetadata] = None
    local_stat: Optional[tuple[int, int, int, int]] = None

    def resolved_object_count(self) -> int:
        """Return the number of exact marketplace-streamed objects this pin owns."""
        if self.kind == "s3" and self.metadata is not None:
            return len((self.metadata,))
        if self.kind == "local" and self.local_path is not None:
            return len((self.local_path,))
        raise ArtifactResolutionError("resolved artifact is incomplete")

    def canonical_locator_bytes(self) -> bytes:
        """Return the exact locator for local HMAC use only."""
        if self.kind == "local" and self.local_path is not None:
            return b"local\x00" + os.fsencode(os.path.realpath(self.local_path))
        if self.kind == "s3" and self.connection is not None and self.metadata is not None:
            return b"s3\x00" + b"\x00".join(
                value.encode("utf-8")
                for value in (
                    self.connection.id,
                    self.connection.bucket,
                    self.metadata.object_key,
                )
            )
        raise ArtifactResolutionError("resolved artifact is incomplete")

    def locator_commitment(self, commitment_key: bytes) -> str:
        if len(commitment_key) < 32:
            raise ArtifactResolutionError("commitment key is invalid")
        return hmac.new(
            commitment_key,
            self.canonical_locator_bytes(),
            hashlib.sha256,
        ).hexdigest()


def _resolve_file_path(
    dataset: DatasetRecord, *, upload_directory: str, processed_directory: str
) -> Optional[str]:
    if dataset.processed_path and os.path.isfile(dataset.processed_path):
        return os.path.realpath(dataset.processed_path)
    standard = os.path.join(processed_directory, f"{dataset.id}.parquet")
    if os.path.isfile(standard):
        return os.path.realpath(standard)
    upload = os.path.join(upload_directory, dataset.storage_filename)
    if os.path.isfile(upload):
        return os.path.realpath(upload)
    return None


def _find_dataset(session, listing_id: str, *, processed_directory: str) -> Optional[DatasetRecord]:
    dataset = session.exec(
        select(DatasetRecord)
        .where(DatasetRecord.listing_id == listing_id)
        .order_by(DatasetRecord.created_at.desc(), DatasetRecord.id.desc())
    ).first()
    if dataset is not None:
        return dataset

    processed_dir = Path(processed_directory)
    if not processed_dir.exists():
        return None
    for result_file in sorted(processed_dir.glob("*/publish_result.json")):
        try:
            with result_file.open(encoding="utf-8") as stream:
                result = json.load(stream)
        except (json.JSONDecodeError, OSError, UnicodeError):
            continue
        if result.get("listing_id") != listing_id:
            continue
        dataset = session.get(DatasetRecord, result_file.parent.name)
        if dataset is not None:
            dataset.listing_id = listing_id
            session.add(dataset)
            session.commit()
            session.refresh(dataset)
            return dataset
    return None


def _detach(session, value):
    session.expunge(value)
    return value


def resolve_source_artifact(
    listing_id: str,
    *,
    upload_directory: Optional[str] = None,
    processed_directory: Optional[str] = None,
    session_context_factory: Callable = get_session_context,
) -> Optional[ResolvedArtifact]:
    """Resolve and pin the exact artifact selected for a registered listing."""
    upload_root = upload_directory or settings.upload_directory
    processed_root = processed_directory or settings.processed_directory
    with session_context_factory() as session:
        dataset = _find_dataset(session, listing_id, processed_directory=processed_root)
        if dataset is None:
            return None

        row = session.exec(
            select(S3Connection, S3ObjectMetadata)
            .where(S3ObjectMetadata.dataset_id == dataset.id)
            .where(S3Connection.id == S3ObjectMetadata.connection_id)
            .order_by(S3ObjectMetadata.id)
        ).first()
        if row is not None:
            connection, metadata = row
            return ResolvedArtifact(
                listing_id=listing_id,
                source_handle_id=dataset.id,
                dataset=_detach(session, dataset),
                kind="s3",
                connection=_detach(session, connection),
                metadata=_detach(session, metadata),
            )

        path = _resolve_file_path(
            dataset,
            upload_directory=upload_root,
            processed_directory=processed_root,
        )
        if path is None:
            return ResolvedArtifact(
                listing_id=listing_id,
                source_handle_id=dataset.id,
                dataset=_detach(session, dataset),
                kind="local",
            )
        stat = os.stat(path, follow_symlinks=True)
        return ResolvedArtifact(
            listing_id=listing_id,
            source_handle_id=dataset.id,
            dataset=_detach(session, dataset),
            kind="local",
            local_path=path,
            local_stat=(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns),
        )


def assert_artifact_still_pinned(artifact: ResolvedArtifact) -> None:
    """Reject replacement or resolver-selection changes before/after a read."""
    current = resolve_source_artifact(artifact.listing_id)
    if current is None or current.kind != artifact.kind:
        raise StaleArtifactIdentityError("registered artifact identity changed")
    if not hmac.compare_digest(current.canonical_locator_bytes(), artifact.canonical_locator_bytes()):
        raise StaleArtifactIdentityError("registered artifact identity changed")
    if artifact.kind == "local" and current.local_stat != artifact.local_stat:
        raise StaleArtifactIdentityError("registered artifact content changed")
    if artifact.kind == "s3":
        assert current.metadata is not None and artifact.metadata is not None
        current_tag = (current.metadata.size_bytes, current.metadata.etag, current.metadata.last_modified)
        pinned_tag = (artifact.metadata.size_bytes, artifact.metadata.etag, artifact.metadata.last_modified)
        if current_tag != pinned_tag:
            raise StaleArtifactIdentityError("registered artifact content changed")
