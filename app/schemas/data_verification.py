"""Wire-exact and local API schemas for the S1590 seller flow."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

import json

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator


OpaqueId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class D6Description(StrictModel):
    domain_class: Literal[
        "education_learning", "software_technology", "business_finance",
        "health_life_sciences", "public_social", "physical_environment",
    ]
    record_granularity: Literal["entity", "event", "measurement", "document", "relationship", "aggregate"]
    temporal_scope: Literal["current_snapshot", "historical_period", "time_series", "mixed_periods", "not_time_based"]
    update_cadence: Literal["one_time", "irregular", "continuous", "daily", "weekly", "monthly", "quarterly", "yearly"]
    intended_use_tags: tuple[
        Literal["analysis_reporting", "research_education", "machine_learning", "benchmarking", "reference_lookup", "operations_planning"], ...
    ] = Field(max_length=5)
    known_limitation_tags: tuple[
        Literal["incomplete_coverage", "missing_values", "estimated_fields", "historical_cutoff", "sampled_source", "known_duplicates", "source_defined_categories"], ...
    ] = Field(max_length=5)

    @field_validator("intended_use_tags", "known_limitation_tags")
    @classmethod
    def _canonical_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("D6 tags must be unique and canonically ordered")
        return value

    @model_validator(mode="after")
    def _bounded_wire_size(self) -> "D6Description":
        encoded = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(encoded) > 2048:
            raise ValueError("D6 exceeds the canonical wire-size bound")
        return self


class HardInferenceBudget(StrictModel):
    max_input_tokens: Literal[8192]
    max_output_tokens: Literal[1024]
    model_request_count: Literal[1]


class QuoteProbeRequest(StrictModel):
    listing_id: UUID
    source_handle_id: OpaqueId
    connector_type: Literal["eolymp"]
    connector_version: Literal["eolymp-v1"]
    owner_consent: Literal[True]
    source_reachable: StrictBool
    objects_discovered: StrictInt = Field(ge=1)
    size_class: Literal["small", "medium", "large"]
    supported_capabilities: tuple[
        Literal[
            "complete_traversal", "deterministic_object_order", "fixed_bucket_aggregates",
            "exact_or_declared_estimated_row_counts",
        ], ...
    ]
    estimated_max_input_tokens: StrictInt = Field(ge=1)
    preview_requested: StrictBool

    @field_validator("supported_capabilities")
    @classmethod
    def _complete_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        required = (
            "complete_traversal", "deterministic_object_order", "fixed_bucket_aggregates",
            "exact_or_declared_estimated_row_counts",
        )
        if value != required:
            raise ValueError("probe capabilities are incomplete or non-canonical")
        return value


class QuoteProbeView(StrictModel):
    source_reachable: StrictBool
    objects_discovered: StrictInt = Field(ge=1)
    fixed_reason_skips: dict[
        Literal["permission_denied", "unsupported_type", "timeout"], StrictInt
    ]
    size_class: Literal["small", "medium", "large"]


class QuoteHardMaximum(StrictModel):
    authorization_usd: Decimal
    inference: HardInferenceBudget


class QuoteResponse(StrictModel):
    quote_id: OpaqueId
    depth_class: Literal["complete_standard_v1"]
    traversal_scope: Literal["all_reachable_supported_objects"]
    row_count_policy: Literal["exact_or_declared_estimate"]
    low_occupancy_behavior: Literal["suppressed_low_occupancy"]
    minimum_aggregate_occupancy: Literal[10]
    hard_maximum: QuoteHardMaximum
    partial_traversal_allowed: Literal[False]


class ScanSpecIssueRequest(StrictModel):
    listing_id: UUID
    source_handle_id: OpaqueId
    owner_authorization_id: OpaqueId
    quote_id: OpaqueId
    idempotency_key: OpaqueId
    accepted_at_utc: datetime
    connector_type: Literal["eolymp"]
    connector_version: Literal["eolymp-v1"]
    depth_class: Literal["complete_standard_v1"]
    preview_requested: StrictBool
    wire_manifest_version: Literal["data-verification-wire-v1"]
    corpus_disclosure_version: Literal["s1396-disclosure-v1"]
    payment_disclosure_version: Literal["payment-disclosure-v1"]
    authorization_usd: Decimal = Decimal("25.00")

    @field_validator("accepted_at_utc")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("accepted_at_utc must include a timezone")
        return value


PaymentState = Literal[
    "CREATED", "QUOTED", "AUTHORIZING", "AUTHORIZED", "SCANNING_LOCAL",
    "NARRATING_CLOUD", "CAPTURE_PENDING", "CAPTURE_RECONCILING", "CAPTURED",
    "PUBLISHED", "DECLINED", "WITHDRAWN", "SUPERSEDED", "AUTH_FAILED",
    "CANCELLED_VOIDED", "FAILED_VOIDED", "CAPTURE_FAILED",
]


class PaymentLifecycleStatus(StrictModel):
    verification_id: OpaqueId
    state: PaymentState
    authorization_usd: Decimal | None
    captured_usd: Decimal | None
    result_available: StrictBool
    publication_allowed: StrictBool
    reconciliation_required: StrictBool
    narrative: str | None = None
    listing_claim_comparison: str | None = None
    withdrawn_at_utc: datetime | None = None

    @field_validator("withdrawn_at_utc")
    @classmethod
    def _withdrawal_timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("withdrawn_at_utc must include a timezone")
        return value


TerminalErrorCode = Literal["permission_denied", "unsupported_type", "timeout", "source_unreachable", "artifact_changed", "scanner_failure"]
NarrativeState = Literal["grounded", "withheld_grounding_failed"]


class ReportIngestResponse(StrictModel):
    verification_id: UUID
    accepted: Literal[True]
    terminal_error_code: TerminalErrorCode | None = None
    narrative_state: NarrativeState | None = None
    narrative: str | None = None
    listing_claim_comparison: str | None = None


class LifecycleCommand(StrictModel):
    verification_id: OpaqueId
    listing_id: UUID
    source_handle_id: OpaqueId
    requested_action: Literal["cancel", "publish", "decline", "withdraw"]


class PrepareVerificationRequest(StrictModel):
    d6_description: D6Description
    preview_requested: StrictBool


class StartVerificationRequest(StrictModel):
    accept_quote: Literal[True]
    publication_terms_ack: Literal[True]
    corpus_ack: Literal[True]


class ConfirmLifecycleRequest(StrictModel):
    confirmed: Literal[True]


class DataVerificationView(StrictModel):
    dataset_id: str
    supported: bool
    unavailable_reason: str | None = None
    run_id: str | None = None
    listing_id: str | None = None
    state: str | None = None
    d6_description: D6Description | None = None
    preview_requested: bool = False
    quote_probe: QuoteProbeView | None = None
    quote: QuoteResponse | None = None
    payment_status: PaymentLifecycleStatus | None = None
    report_ingest: ReportIngestResponse | None = None
    findings: dict[str, Any] | None = None
    d8_preview: list[dict[str, Any]] | None = None
    active_publication: dict[str, Any] | None = None
