"""Customer-side D6 fixed-enum sanitizer."""

from __future__ import annotations

import unicodedata
from typing import Any

from app.services.marketplace_action_signer import canonical_json_bytes


class D6RejectedError(ValueError):
    """Fixed-message rejection that never includes the submitted value."""


SINGLE_ENUMS = {
    "domain_class": frozenset(
        {
            "education_learning", "software_technology", "business_finance",
            "health_life_sciences", "public_social", "physical_environment",
        }
    ),
    "record_granularity": frozenset(
        {"entity", "event", "measurement", "document", "relationship", "aggregate"}
    ),
    "temporal_scope": frozenset(
        {"current_snapshot", "historical_period", "time_series", "mixed_periods", "not_time_based"}
    ),
    "update_cadence": frozenset(
        {"one_time", "irregular", "continuous", "daily", "weekly", "monthly", "quarterly", "yearly"}
    ),
}
TAG_ENUMS = {
    "intended_use_tags": frozenset(
        {"analysis_reporting", "research_education", "machine_learning", "benchmarking", "reference_lookup", "operations_planning"}
    ),
    "known_limitation_tags": frozenset(
        {"incomplete_coverage", "missing_values", "estimated_fields", "historical_cutoff", "sampled_source", "known_duplicates", "source_defined_categories"}
    ),
}
ALLOWED_KEYS = frozenset((*SINGLE_ENUMS, *TAG_ENUMS))


def _normalize_enum(value: Any) -> str:
    if not isinstance(value, str):
        raise D6RejectedError("D6 value is invalid")
    normalized = unicodedata.normalize("NFKC", value)
    if normalized != value:
        raise D6RejectedError("D6 value is invalid")
    return normalized


def sanitize_d6(candidate: Any) -> dict[str, Any]:
    """Return canonical D6 or reject without echoing any hostile content."""
    if not isinstance(candidate, dict) or set(candidate) != ALLOWED_KEYS:
        raise D6RejectedError("D6 schema is invalid")
    clean: dict[str, Any] = {}
    for key, allowed in SINGLE_ENUMS.items():
        value = _normalize_enum(candidate[key])
        if value not in allowed:
            raise D6RejectedError("D6 value is invalid")
        clean[key] = value
    for key, allowed in TAG_ENUMS.items():
        values = candidate[key]
        if not isinstance(values, list) or len(values) > 5:
            raise D6RejectedError("D6 tags are invalid")
        normalized = [_normalize_enum(item) for item in values]
        if any(item not in allowed for item in normalized):
            raise D6RejectedError("D6 tags are invalid")
        clean[key] = sorted(set(normalized))
    if len(canonical_json_bytes(clean)) > 2048:
        raise D6RejectedError("D6 size is invalid")
    return clean
