import os
from datetime import datetime, timezone
from itertools import combinations
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.database import get_session_context
from app.models.dataset import DatasetRecord
from app.models.s3_connection import S3Connection
from app.models.s3_object_metadata import S3ObjectMetadata
from app.models.s3_scan_job import S3ScanJob
from app.services import source_artifact_resolver as resolver
from app.services.data_verification.connectors.eolymp_v1 import object_commitment
from app.services.marketplace_action_signer import canonical_json_bytes


def _dataset(*, listing_id: str, dataset_id: str, filename: str, processed_path=None):
    record = DatasetRecord(
        id=dataset_id,
        original_filename=filename,
        storage_filename=filename,
        file_type="csv",
        status="preview_ready",
        listing_id=listing_id,
        processed_path=processed_path,
    )
    with get_session_context() as session:
        session.add(record)
        session.commit()
    return record


def test_local_resolver_pins_preferred_artifact_and_detects_selection_change(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    processed = tmp_path / "processed"
    uploads.mkdir()
    processed.mkdir()
    monkeypatch.setattr(resolver.settings, "upload_directory", str(uploads))
    monkeypatch.setattr(resolver.settings, "processed_directory", str(processed))
    dataset_id = f"ds-{uuid4()}"
    listing_id = f"listing-{uuid4()}"
    upload = uploads / "source.csv"
    upload.write_bytes(b"id\n1\n")
    _dataset(listing_id=listing_id, dataset_id=dataset_id, filename=upload.name)

    first = resolver.resolve_source_artifact(listing_id)
    assert first is not None and first.local_path == os.path.realpath(upload)
    first_commitment = first.locator_commitment(b"c" * 32)

    preferred = processed / f"{dataset_id}.parquet"
    preferred.write_bytes(b"PAR1different")
    second = resolver.resolve_source_artifact(listing_id)
    assert second is not None
    assert second.locator_commitment(b"c" * 32) != first_commitment
    with pytest.raises(resolver.StaleArtifactIdentityError):
        resolver.assert_artifact_still_pinned(first)


def test_local_resolver_detects_in_place_content_replacement(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    processed = tmp_path / "processed"
    uploads.mkdir()
    processed.mkdir()
    monkeypatch.setattr(resolver.settings, "upload_directory", str(uploads))
    monkeypatch.setattr(resolver.settings, "processed_directory", str(processed))
    path = uploads / "source.csv"
    path.write_bytes(b"id\n1\n")
    listing_id = f"listing-{uuid4()}"
    _dataset(listing_id=listing_id, dataset_id=f"ds-{uuid4()}", filename=path.name)
    pinned = resolver.resolve_source_artifact(listing_id)
    path.write_bytes(b"id\n2\n3\n")
    with pytest.raises(resolver.StaleArtifactIdentityError):
        resolver.assert_artifact_still_pinned(pinned)


def test_s3_resolver_commitment_changes_with_hmac_key_and_registered_object(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver.settings, "upload_directory", str(tmp_path / "uploads"))
    monkeypatch.setattr(resolver.settings, "processed_directory", str(tmp_path / "processed"))
    dataset_id = f"ds-{uuid4()}"
    listing_id = f"listing-{uuid4()}"
    _dataset(listing_id=listing_id, dataset_id=dataset_id, filename="unused.csv")
    connection = S3Connection(
        id=f"conn-{uuid4()}", name="registered", bucket="bucket-a", region="us-east-1",
        role_arn="arn:aws:iam::123456789012:role/aim-data", external_id=str(uuid4()), status="configured",
    )
    job = S3ScanJob(id=f"scan-{uuid4()}", connection_id=connection.id)
    metadata = S3ObjectMetadata(
        id=f"obj-{uuid4()}", connection_id=connection.id, scan_job_id=job.id,
        object_key="registered/source.csv", size_bytes=10, content_type="text/csv",
        last_modified=datetime.now(timezone.utc), etag="etag-a", dataset_id=dataset_id,
    )
    metadata_id = metadata.id
    with get_session_context() as session:
        session.add(connection)
        session.add(job)
        session.add(metadata)
        session.commit()
    pinned = resolver.resolve_source_artifact(listing_id)
    assert pinned is not None
    before = pinned.locator_commitment(b"c" * 32)
    assert pinned.locator_commitment(b"d" * 32) != before
    with get_session_context() as session:
        row = session.get(S3ObjectMetadata, metadata_id)
        row.object_key = "registered/changed.csv"
        session.add(row)
        session.commit()
    changed = resolver.resolve_source_artifact(listing_id)
    assert changed.locator_commitment(b"c" * 32) != before
    with pytest.raises(resolver.StaleArtifactIdentityError):
        resolver.assert_artifact_still_pinned(pinned)


def test_cloud_visible_commitments_offer_no_dictionary_equality_or_ordering_oracle():
    dataset = SimpleNamespace()
    artifacts = [
        resolver.ResolvedArtifact(
            listing_id="listing-local-1",
            source_handle_id="dataset-local-1",
            dataset=dataset,
            kind="local",
            local_path="/srv/aim-data/customers/acme/orders-2026.parquet",
        ),
        resolver.ResolvedArtifact(
            listing_id="listing-local-2",
            source_handle_id="dataset-local-2",
            dataset=dataset,
            kind="local",
            local_path="/Users/alice/Library/Application Support/AIM Data/private/customers.csv",
        ),
        resolver.ResolvedArtifact(
            listing_id="listing-s3-1",
            source_handle_id="dataset-s3-1",
            dataset=dataset,
            kind="s3",
            connection=SimpleNamespace(id="conn-prod-eu-west-1", bucket="acme-private-analytics"),
            metadata=SimpleNamespace(object_key="exports/2026/08/customers.parquet"),
        ),
        resolver.ResolvedArtifact(
            listing_id="listing-s3-2",
            source_handle_id="dataset-s3-2",
            dataset=dataset,
            kind="s3",
            connection=SimpleNamespace(id="conn-finance-us-east-1", bucket="finance-ledger-private"),
            metadata=SimpleNamespace(object_key="daily/2026-08-24/ledger.jsonl"),
        ),
    ]
    object_names = [
        "warehouse_prod.public.customers",
        "analytics.sales.orders_2026",
        "archives/2026-08-24/customers.csv",
        "registered-root",
        "objects/private/customer-events.jsonl",
    ]
    commitment_keys = [bytes([value]) * 32 for value in (11, 22, 33)]

    commitments_by_key = []
    for key in commitment_keys:
        commitments_by_key.append(
            [artifact.locator_commitment(key) for artifact in artifacts]
            + [object_commitment(key, b"dataset-handle-private", name) for name in object_names]
        )

    visible = canonical_json_bytes(commitments_by_key)
    raw_dictionary = [
        artifact.canonical_locator_bytes() for artifact in artifacts
    ] + [name.encode("utf-8") for name in object_names]
    assert all(candidate not in visible for candidate in raw_dictionary)

    for position in range(len(commitments_by_key[0])):
        assert len({commitments[position] for commitments in commitments_by_key}) == len(commitment_keys)
    for left, right in combinations(commitments_by_key, 2):
        assert set(left).isdisjoint(right)
        assert any(
            (left[first] < left[second]) != (right[first] < right[second])
            for first in range(len(left))
            for second in range(first + 1, len(left))
        )

    attacker_keys = [bytes([value]) * 32 for value in (44, 55)]
    attacker_guesses = {
        artifact.locator_commitment(key)
        for key in attacker_keys
        for artifact in artifacts
    } | {
        object_commitment(key, b"dataset-handle-private", name)
        for key in attacker_keys
        for name in object_names
    }
    assert all(attacker_guesses.isdisjoint(commitments) for commitments in commitments_by_key)
