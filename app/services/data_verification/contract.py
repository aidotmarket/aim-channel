"""Strict scan-spec v1 contract and canonicalization."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from typing import Any, Literal, MutableSet, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.services.marketplace_action_signer import canonical_json_bytes


class ContractError(ValueError):
    """A fixed-message contract rejection safe for customer-side logs."""


FACT_FIELD_CONTRACT = (
    "coverage",
    "objects.object_id",
    "objects.column_names",
    "objects.column_types",
    "objects.null_rate",
    "objects.approx_distinct_count",
    "objects.length_histograms",
    "objects.numeric_range_buckets",
    "objects.row_count",
    "objects.row_count_method",
    "fingerprint_hash",
)

REPORT_FIELD_CONTRACT = frozenset(
    {
        "verification_id", "listing_id", "owner_authorization_id", "quote_id",
        "idempotency_key", "wire_manifest_version", "corpus_disclosure_version",
        "payment_disclosure_version", "accepted_at_utc", "requested_action",
        "source_handle_id", "artifact_locator_commitment", "content_sha256",
        "spec_id", "spec_version", "spec_hash", "nonce_echo", "install_key_id",
        "agent_version", "connector_type", "connector_version", "started_at_utc",
        "completed_at_utc", "duration_ms", "depth_class",
        "row_count_algorithm_version", "distinct_algorithm_version",
        "histogram_version", "numeric_bucket_version", "coverage", "objects",
        "fingerprint_hash", "canonicalization_version", "receipt_signature",
        "signature_algorithm", "d6_description", "preview_requested",
    }
)


def assert_report_field_contract(report: dict[str, Any]) -> None:
    if set(report) != REPORT_FIELD_CONTRACT:
        raise ContractError("verification report field contract is invalid")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HardInferenceBudget(StrictModel):
    max_input_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    model_request_count: Literal[1]


class BucketDefinitions(StrictModel):
    string_length_upper_bounds: tuple[int, ...]
    numeric_boundaries: tuple[float, ...]

    @field_validator("string_length_upper_bounds")
    @classmethod
    def _ascending_lengths(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or tuple(sorted(set(value))) != value or value[0] < 0:
            raise ValueError("length bucket policy is invalid")
        return value

    @field_validator("numeric_boundaries")
    @classmethod
    def _ascending_numbers(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if tuple(sorted(set(value))) != value:
            raise ValueError("numeric bucket policy is invalid")
        return value


class CancellationSignal(StrictModel):
    kind: Literal["signed_spec_flag"]
    cancelled: bool


class ScanSpecPayload(StrictModel):
    spec_id: str = Field(min_length=1, max_length=128)
    spec_version: Literal["1"]
    verification_id: str = Field(min_length=1, max_length=128)
    listing_id: str = Field(min_length=1, max_length=255)
    owner_authorization_id: str = Field(min_length=1, max_length=128)
    quote_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    accepted_at_utc: datetime
    requested_action: Literal["start"]
    source_handle_id: str = Field(min_length=1, max_length=128)
    wire_manifest_version: Literal["data-verification-wire-v1"]
    corpus_disclosure_version: str = Field(min_length=1, max_length=128)
    payment_disclosure_version: str = Field(min_length=1, max_length=128)
    connector_type: Literal["eolymp"]
    connector_version: Literal["eolymp-v1"]
    traversal_root: Literal["registered_source_artifact"]
    traversal_order: Literal["canonical_object_identity_ascending"]
    fingerprint_algorithm: Literal["sha256"]
    canonicalization_version: Literal["python-json-sort-compact-v1"]
    approximate_distinct_algorithm: Literal["hll-sha256-v1"]
    deterministic_seed: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_contract: Literal["data-verification-report-v1"]
    depth_class: Literal["complete_standard_v1"]
    field_contract: tuple[str, ...]
    bucket_definitions: BucketDefinitions
    minimum_aggregate_occupancy: int = Field(ge=3)
    low_occupancy_behavior: Literal["suppressed_low_occupancy"]
    row_count_algorithm_version: Literal["exact-v1"]
    histogram_version: Literal["fixed-buckets-v1"]
    numeric_bucket_version: Literal["fixed-buckets-v1"]
    d6_schema_version: Literal["d6-v1"]
    d6_sanitizer_policy_version: Literal["nfkc-fixed-enum-v1"]
    hard_inference_budget: HardInferenceBudget
    preview_requested: bool
    issued_at_utc: datetime
    expires_at_utc: datetime
    cancellation_signal: CancellationSignal
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")
    platform_key_id: str = Field(min_length=1, max_length=128)

    @field_validator("field_contract")
    @classmethod
    def _exact_field_contract(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != FACT_FIELD_CONTRACT:
            raise ValueError("fact field contract is invalid")
        return value

    @model_validator(mode="after")
    def _valid_times(self):
        if self.accepted_at_utc > self.issued_at_utc or self.expires_at_utc <= self.issued_at_utc:
            raise ValueError("scan spec time window is invalid")
        return self


class SignedScanSpec(StrictModel):
    payload: ScanSpecPayload
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_algorithm: Literal["RSASSA_PKCS1_V1_5_SHA256"]
    spec_signature: str = Field(min_length=1, max_length=4096)


def parse_and_verify_scan_spec(
    document: dict[str, Any],
    *,
    platform_public_key: bytes | rsa.RSAPublicKey,
    now: Optional[datetime] = None,
    seen_nonces: Optional[MutableSet[str]] = None,
) -> ScanSpecPayload:
    """Validate canonical shape, hash, signature, freshness, and cancellation."""
    try:
        spec = SignedScanSpec.model_validate(document)
    except ValidationError as exc:
        raise ContractError("scan spec schema is invalid") from exc

    payload_dict = spec.payload.model_dump(mode="json")
    payload_bytes = canonical_json_bytes(payload_dict)
    actual_hash = hashlib.sha256(payload_bytes).hexdigest()
    if actual_hash != spec.spec_hash:
        raise ContractError("scan spec hash is invalid")
    try:
        signature = base64.b64decode(spec.spec_signature, validate=True)
        key = platform_public_key
        if isinstance(platform_public_key, bytes):
            key = serialization.load_pem_public_key(platform_public_key)
        if not isinstance(key, rsa.RSAPublicKey):
            raise ValueError("platform key is not RSA")
        key.verify(signature, payload_bytes, padding.PKCS1v15(), hashes.SHA256())
    except (TypeError, ValueError, InvalidSignature) as exc:
        raise ContractError("scan spec signature is invalid") from exc

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if current < spec.payload.issued_at_utc or current >= spec.payload.expires_at_utc:
        raise ContractError("scan spec is not active")
    if spec.payload.cancellation_signal.cancelled:
        raise ContractError("scan was cancelled")
    if seen_nonces is not None:
        if spec.payload.nonce in seen_nonces:
            raise ContractError("scan spec nonce was replayed")
        seen_nonces.add(spec.payload.nonce)
    return spec.payload
