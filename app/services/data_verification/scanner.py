"""Local deterministic scan orchestration and receipt construction."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from cryptography.hazmat.primitives import serialization

from app.services.data_verification.connectors.eolymp_v1 import (
    BudgetIncompatibleError,
    EolympConnectorV1,
    IncompleteCoverageError,
    UnsupportedConnectorShape,
)
from app.services.data_verification.contract import (
    assert_report_field_contract,
    parse_and_verify_scan_spec,
)
from app.services.data_verification.sanitizer import sanitize_d6
from app.services.marketplace_action_signer import (
    canonical_json_bytes,
    sign_receipt_payload,
)
from app.services.s3_broker_client import S3BrokerClient, S3BrokerError
from app.services.source_artifact_resolver import (
    ArtifactResolutionError,
    ResolvedArtifact,
    assert_artifact_still_pinned,
    resolve_source_artifact,
)


AGENT_VERSION = "aim-data-verification-v1"


class ScanRefusedError(RuntimeError):
    """Fixed-message refusal with no source content."""


@dataclass(frozen=True)
class ScanExecution:
    report: dict[str, Any]
    d8_projection: Optional[list[dict[str, Any]]]


def receipt_signature_binding(report: dict[str, Any]) -> dict[str, Any]:
    """The exact Gate 1 receipt fields covered by the install signature."""
    return {
        "spec_hash": report["spec_hash"],
        "nonce_echo": report["nonce_echo"],
        "install_key_id": report["install_key_id"],
        "artifact_locator_commitment": report["artifact_locator_commitment"],
        "content_sha256": report["content_sha256"],
        "started_at_utc": report["started_at_utc"],
        "completed_at_utc": report["completed_at_utc"],
        "duration_ms": report["duration_ms"],
        "coverage": report["coverage"],
        "fingerprint_hash": report["fingerprint_hash"],
    }


class DataVerificationScanner:
    def __init__(
        self,
        *,
        commitment_key: bytes,
        install_private_key,
        install_key_id: str,
        platform_public_key: bytes,
        broker_client: Optional[S3BrokerClient] = None,
        clock: Optional[Callable[[], datetime]] = None,
        seen_nonces: Optional[set[str]] = None,
    ) -> None:
        if len(commitment_key) < 32:
            raise ValueError("commitment key is invalid")
        raw_install_key = install_private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        if hmac.compare_digest(commitment_key[:32], raw_install_key):
            raise ValueError("commitment and receipt signing keys must be distinct")
        self._commitment_key = bytes(commitment_key)
        self._install_private_key = install_private_key
        self._install_key_id = install_key_id
        self._platform_public_key = platform_public_key
        self._broker = broker_client or S3BrokerClient()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._seen_nonces = seen_nonces if seen_nonces is not None else set()

    def _read_artifact(self, artifact: ResolvedArtifact) -> tuple[str, bytes, str]:
        assert_artifact_still_pinned(artifact)
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        try:
            if artifact.kind == "local":
                if artifact.local_path is None:
                    raise ScanRefusedError("registered artifact is unavailable")
                artifact_name = Path(artifact.local_path).name
                with open(artifact.local_path, "rb") as stream:
                    while chunk := stream.read(65536):
                        digest.update(chunk)
                        chunks.append(chunk)
            else:
                if artifact.connection is None or artifact.metadata is None or not artifact.connection.role_arn:
                    raise ScanRefusedError("registered artifact is unavailable")
                artifact_name = artifact.metadata.object_key
                for chunk in self._broker.stream_registered_artifact(
                    source_handle_id=artifact.source_handle_id,
                ):
                    digest.update(chunk)
                    chunks.append(chunk)
        except (OSError, S3BrokerError, ArtifactResolutionError) as exc:
            raise ScanRefusedError("registered artifact could not be read") from exc
        assert_artifact_still_pinned(artifact)
        return artifact_name, b"".join(chunks), digest.hexdigest()

    def scan(
        self,
        *,
        signed_spec: dict[str, Any],
        d6_candidate: Any,
        now: Optional[datetime] = None,
    ) -> ScanExecution:
        # D6 must fail before resolution, signing, broker, or any HTTP client.
        d6 = sanitize_d6(d6_candidate)
        spec = parse_and_verify_scan_spec(
            signed_spec,
            platform_public_key=self._platform_public_key,
            now=now,
            seen_nonces=self._seen_nonces,
        )
        artifact = resolve_source_artifact(spec.listing_id)
        if artifact is None or artifact.source_handle_id != spec.source_handle_id:
            raise ScanRefusedError("registered source handle does not match the signed spec")

        started = self._clock().astimezone(timezone.utc)
        artifact_name, payload, content_hash = self._read_artifact(artifact)
        connector = EolympConnectorV1()
        try:
            facts = connector.scan_bytes(
                artifact_name=artifact_name,
                payload=payload,
                commitment_key=self._commitment_key,
                source_binding=spec.source_handle_id.encode("utf-8"),
                deterministic_seed=spec.deterministic_seed,
                minimum_aggregate_occupancy=spec.minimum_aggregate_occupancy,
                length_bounds=spec.bucket_definitions.string_length_upper_bounds,
                numeric_boundaries=spec.bucket_definitions.numeric_boundaries,
                max_inference_input_tokens=spec.hard_inference_budget.max_input_tokens,
                preview_requested=spec.preview_requested,
            )
        except (UnsupportedConnectorShape, IncompleteCoverageError, BudgetIncompatibleError) as exc:
            raise ScanRefusedError(str(exc)) from exc
        completed = self._clock().astimezone(timezone.utc)
        duration_ms = max(0, int((completed - started).total_seconds() * 1000))

        fingerprint = {
            "canonicalization_version": spec.canonicalization_version,
            "depth_class": spec.depth_class,
            "row_count_algorithm_version": spec.row_count_algorithm_version,
            "distinct_algorithm_version": spec.approximate_distinct_algorithm,
            "histogram_version": spec.histogram_version,
            "numeric_bucket_version": spec.numeric_bucket_version,
            "coverage": facts["coverage"],
            "objects": facts["objects"],
        }
        fingerprint_hash = hashlib.sha256(canonical_json_bytes(fingerprint)).hexdigest()
        report: dict[str, Any] = {
            "verification_id": spec.verification_id,
            "listing_id": spec.listing_id,
            "owner_authorization_id": spec.owner_authorization_id,
            "quote_id": spec.quote_id,
            "idempotency_key": spec.idempotency_key,
            "wire_manifest_version": spec.wire_manifest_version,
            "corpus_disclosure_version": spec.corpus_disclosure_version,
            "payment_disclosure_version": spec.payment_disclosure_version,
            "accepted_at_utc": spec.accepted_at_utc.isoformat().replace("+00:00", "Z"),
            "requested_action": spec.requested_action,
            "source_handle_id": spec.source_handle_id,
            "artifact_locator_commitment": artifact.locator_commitment(self._commitment_key),
            "content_sha256": content_hash,
            "spec_id": spec.spec_id,
            "spec_version": spec.spec_version,
            "spec_hash": signed_spec["spec_hash"],
            "nonce_echo": spec.nonce,
            "install_key_id": self._install_key_id,
            "agent_version": AGENT_VERSION,
            "connector_type": spec.connector_type,
            "connector_version": spec.connector_version,
            "started_at_utc": started.isoformat().replace("+00:00", "Z"),
            "completed_at_utc": completed.isoformat().replace("+00:00", "Z"),
            "duration_ms": duration_ms,
            "depth_class": spec.depth_class,
            "row_count_algorithm_version": spec.row_count_algorithm_version,
            "distinct_algorithm_version": spec.approximate_distinct_algorithm,
            "histogram_version": spec.histogram_version,
            "numeric_bucket_version": spec.numeric_bucket_version,
            "coverage": facts["coverage"],
            "objects": facts["objects"],
            "fingerprint_hash": fingerprint_hash,
            "canonicalization_version": spec.canonicalization_version,
            "d6_description": d6,
            "preview_requested": spec.preview_requested,
            "signature_algorithm": "Ed25519",
        }
        report["receipt_signature"] = sign_receipt_payload(
            receipt_signature_binding(report), self._install_private_key
        )
        assert_report_field_contract(report)
        return ScanExecution(report=report, d8_projection=facts["schema_preview"])
