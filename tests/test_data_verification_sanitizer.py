import json
from pathlib import Path

import pytest

from app.services.data_verification.sanitizer import D6RejectedError, sanitize_d6


FIXTURES = Path(__file__).parent / "fixtures" / "data_verification_v1"


def valid_d6():
    return {
        "domain_class": "education_learning",
        "record_granularity": "entity",
        "temporal_scope": "current_snapshot",
        "update_cadence": "one_time",
        "intended_use_tags": ["research_education", "analysis_reporting", "analysis_reporting"],
        "known_limitation_tags": [],
    }


def test_d6_canonicalizes_only_stable_enum_array_order():
    assert sanitize_d6(valid_d6())["intended_use_tags"] == [
        "analysis_reporting",
        "research_education",
    ]


def test_every_hostile_d6_fixture_is_rejected_without_echo():
    cases = json.loads((FIXTURES / "hostile_d6.json").read_text())["cases"]
    for case in cases:
        with pytest.raises(D6RejectedError) as exc_info:
            sanitize_d6(case["value"])
        assert "secret" not in str(exc_info.value).lower()
        assert "password" not in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra="x"),
        lambda value: value.update(domain_class=1),
        lambda value: value.update(intended_use_tags=["analysis_reporting"] * 6),
        lambda value: value.update(known_limitation_tags=[{"nested": "value"}]),
    ],
)
def test_unknown_keys_types_counts_and_nested_values_fail_closed(mutation):
    candidate = valid_d6()
    mutation(candidate)
    with pytest.raises(D6RejectedError):
        sanitize_d6(candidate)
