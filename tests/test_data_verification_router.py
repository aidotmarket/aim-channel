import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from sqlmodel import SQLModel

from app.auth.api_key_auth import AuthenticatedUser, get_current_user
from app.core.database import get_engine, get_session_context
from app.models.dataset import DatasetRecord
from app.models.data_verification import DataVerificationRun
from app.routers.data_verification import router
from app.schemas.data_verification import LifecycleCommand, QuoteProbeRequest, ScanSpecIssueRequest
from app.services.data_verification_client import DataVerificationClient, DataVerificationClientError
from app.services.marketplace_action_signer import canonical_json_bytes, canonical_payload_hash


FIXTURES = Path(__file__).parent / "fixtures" / "data_verification_v1"
LISTING_ID = "11111111-1111-4111-8111-111111111111"
VERIFICATION_ID = "22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
async def test_lifecycle_client_contract_paths_claims_and_canonical_payloads():
    contract = json.loads((FIXTURES / "lifecycle_client_contract.json").read_text())
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        route_name = request.url.path.rsplit("/", 1)[-1]
        authorization = request.headers.get("Authorization", "")
        claims = None
        if request.content:
            body = json.loads(request.content)
            assert request.content == canonical_json_bytes(body)
        if authorization.startswith("Bearer ") and authorization != "Bearer seller-token":
            claims = jwt.decode(authorization[7:], options={"verify_signature": False})
            assert claims["payload_hash"] == canonical_payload_hash(body)
        seen.append((request.method, request.url.path, claims and claims["action"]))
        if route_name == "readiness":
            return httpx.Response(200, json={
                "version": "data_verification_payin_readiness_v1",
                "state": "setup_required",
                "can_start_setup": True,
                "can_replace_payment_method": False,
                "message": "Add a card only when starting paid data verification.",
            })
        if route_name == "quote":
            return httpx.Response(200, json={
                "quote_id": "quote_fixture", "depth_class": "complete_standard_v1",
                "traversal_scope": "all_reachable_supported_objects", "row_count_policy": "exact_or_declared_estimate",
                "low_occupancy_behavior": "suppressed_low_occupancy", "minimum_aggregate_occupancy": 10,
                "hard_maximum": {"authorization_usd": "25.00", "inference": {"max_input_tokens": 8192, "max_output_tokens": 1024, "model_request_count": 1}},
                "partial_traversal_allowed": False,
            })
        if route_name == "scan-spec" and request.method == "POST":
            return httpx.Response(200, json=json.loads((FIXTURES / "scan_spec.json").read_text()))
        if route_name == "scan-spec" and request.method == "PUT":
            return httpx.Response(200, json={
                "verification_id": VERIFICATION_ID,
                "accepted": True,
                "terminal_error_code": None,
                "narrative_state": "grounded",
                "narrative": "Grounded allAI narrative fixture.",
                "listing_claim_comparison": "Listing claims match the deterministic scan fixture.",
            })
        response_headers = {
            "Date": "invalid-date" if route_name == "withdraw" else "Sat, 22 Aug 2026 18:30:00 GMT"
        }
        result_available = route_name != "status"
        return httpx.Response(200, headers=response_headers, json={
            "verification_id": VERIFICATION_ID,
            "state": "CAPTURED" if result_available else "NARRATING_CLOUD",
            "authorization_usd": "25.00",
            "captured_usd": "1.00" if result_available else None,
            "result_available": result_available,
            "publication_allowed": result_available,
            "reconciliation_required": False,
            "narrative": "Grounded allAI narrative fixture." if result_available else None,
            "listing_claim_comparison": "Listing claims match the deterministic scan fixture." if result_available else None,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DataVerificationClient(
            base_url="https://backend.example",
            seller_id="seller_fixture",
            install_id="install_fixture",
            install_private_key=Ed25519PrivateKey.generate(),
            seller_access_token="seller-token",
            http_client=http_client,
        )
        readiness = await client.payment_method_readiness()
        await client.quote(QuoteProbeRequest(
            listing_id=LISTING_ID,
            source_handle_id="dataset_fixture",
            connector_type="eolymp",
            connector_version="eolymp-v1",
            owner_consent=True,
            source_reachable=True,
            objects_discovered=1,
            size_class="small",
            supported_capabilities=("complete_traversal", "deterministic_object_order", "fixed_bucket_aggregates", "exact_or_declared_estimated_row_counts"),
            estimated_max_input_tokens=512,
            preview_requested=True,
        ))
        await client.start(ScanSpecIssueRequest(
            listing_id=LISTING_ID,
            source_handle_id="dataset_fixture",
            owner_authorization_id="authorization_fixture",
            quote_id="quote_fixture",
            idempotency_key="idempotency_fixture",
            accepted_at_utc=datetime(2026, 8, 22, tzinfo=timezone.utc),
            connector_type="eolymp",
            connector_version="eolymp-v1",
            depth_class="complete_standard_v1",
            preview_requested=True,
            wire_manifest_version="data-verification-wire-v1",
            corpus_disclosure_version="s1396-disclosure-v1",
            payment_disclosure_version="payment-disclosure-v1",
        ))
        ingest = await client.ingest_report(json.loads((FIXTURES / "report.json").read_text()))
        status = await client.status(VERIFICATION_ID)
        command_results = {}
        for action in ("cancel", "publish", "decline", "withdraw"):
            command_results[action] = await client.command(LifecycleCommand(
                verification_id=VERIFICATION_ID,
                listing_id=LISTING_ID,
                source_handle_id="dataset_fixture",
                requested_action=action,
            ))

    expected = contract["routes"]
    requirements = contract["chunk_6_response_requirements"]
    assert readiness == "setup_required"
    assert ingest.narrative == "Grounded allAI narrative fixture."
    assert ingest.listing_claim_comparison == "Listing claims match the deterministic scan fixture."
    assert status.state == "NARRATING_CLOUD"
    assert status.result_available is False
    assert status.narrative is None
    assert status.listing_claim_comparison is None
    for result in command_results.values():
        assert result.status.state == "CAPTURED"
        assert result.status.result_available is True
        assert result.status.narrative == "Grounded allAI narrative fixture."
        assert result.status.listing_claim_comparison == "Listing claims match the deterministic scan fixture."
    assert command_results["withdraw"].server_date_utc is None
    assert requirements["grounded_result_projection"]["must_carry_display_safe_fields"] == [
        "narrative",
        "listing_claim_comparison",
    ]
    assert requirements["PaymentLifecycleStatus"]["must_carry_field"] == "withdrawn_at_utc"
    assert requirements["carried_item"]["id"] == "s3_pinned_archive_member_enumeration"
    assert seen == [
        ("GET", "/api/v1/data-verification/payment-method/readiness", None),
        (expected["quote"]["method"], expected["quote"]["path_template"], expected["quote"]["expected_action"]),
        (expected["start"]["method"], expected["start"]["path_template"], expected["start"]["expected_action"]),
        (expected["report_ingest"]["method"], expected["report_ingest"]["path_template"], None),
        (expected["status"]["method"], expected["status"]["path_template"].format(verification_id=VERIFICATION_ID), None),
        *[
            (expected[action]["method"], expected[action]["path_template"].format(verification_id=VERIFICATION_ID), expected[action]["expected_action"])
            for action in ("cancel", "publish", "decline", "withdraw")
        ],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "can_start_setup", "can_replace_payment_method"),
    [
        ("setup_required", 1, 0),
        ("setup_pending", 0, 0),
        ("ready", 0, 1),
        ("blocked", 0, 0),
    ],
)
async def test_payment_readiness_rejects_integer_boolean_substitutes(
    state, can_start_setup, can_replace_payment_method
):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "version": "data_verification_payin_readiness_v1",
                "state": state,
                "can_start_setup": can_start_setup,
                "can_replace_payment_method": can_replace_payment_method,
                "message": "invalid integer flags",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DataVerificationClient(
            base_url="https://backend.example",
            seller_id="seller_fixture",
            install_id="install_fixture",
            install_private_key=Ed25519PrivateKey.generate(),
            seller_access_token="seller-token",
            http_client=http_client,
        )
        with pytest.raises(
            DataVerificationClientError,
            match="ai.market returned an invalid payment-readiness response",
        ):
            await client.payment_method_readiness()


@pytest.mark.asyncio
async def test_client_does_not_reflect_backend_response_body():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "private/source.csv source exception"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = DataVerificationClient(
            base_url="https://backend.example",
            seller_id="seller_fixture",
            install_id="install_fixture",
            install_private_key=Ed25519PrivateKey.generate(),
            seller_access_token="seller-token",
            http_client=http_client,
        )
        with pytest.raises(Exception) as exc_info:
            await client.status(VERIFICATION_ID)
    assert "private/source.csv" not in str(exc_info.value)
    assert "422" in str(exc_info.value)


@pytest.mark.asyncio
async def test_disabled_flag_projects_only_identity_support_and_reason_on_every_route(monkeypatch):
    monkeypatch.setenv("DATA_VERIFICATION_ENABLED", "false")
    SQLModel.metadata.create_all(get_engine())
    dataset_ids = ("router-s1590-captured", "router-s1590-published")
    with get_session_context() as session:
        for dataset_id, state in zip(dataset_ids, ("CAPTURED", "PUBLISHED"), strict=True):
            if session.get(DatasetRecord, dataset_id) is None:
                session.add(DatasetRecord(
                    id=dataset_id,
                    original_filename="source.csv",
                    storage_filename="source.csv",
                    file_type="csv",
                    status="preview_ready",
                    listing_id=LISTING_ID,
                ))
                session.add(DataVerificationRun(
                    dataset_id=dataset_id,
                    listing_id=LISTING_ID,
                    source_handle_id=dataset_id,
                    verification_id=f"verification_{state.lower()}",
                    state=state,
                    idempotency_key=f"idempotency_{state.lower()}",
                    owner_authorization_id=f"authorization_{state.lower()}",
                    accepted_at_utc=datetime(2026, 8, 22, tzinfo=timezone.utc),
                    preview_requested=True,
                    publication_terms_ack=True,
                    corpus_ack=True,
                    d6_json=json.dumps({
                        "domain_class": "education_learning",
                        "record_granularity": "entity",
                        "temporal_scope": "current_snapshot",
                        "update_cadence": "one_time",
                        "intended_use_tags": [],
                        "known_limitation_tags": [],
                    }),
                    probe_json=json.dumps({
                        "source_reachable": True,
                        "objects_discovered": 1,
                        "fixed_reason_skips": {},
                        "size_class": "small",
                    }),
                    quote_json=json.dumps({"sensitive_quote": "must-not-render"}),
                    payment_status_json=json.dumps({"sensitive_payment": "must-not-render"}),
                    report_json=json.dumps({"sensitive_findings": "must-not-render"}),
                    d8_json=json.dumps([{"sensitive_d8": "must-not-render"}]),
                ))
        session.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="seller", key_id="test", scopes=["read", "write"], valid=True
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for dataset_id in dataset_ids:
            expected = {
                "dataset_id": dataset_id,
                "supported": False,
                "unavailable_reason": "Data verification is not enabled on this AIM Data installation.",
            }
            calls = (
                ("GET", f"/api/data-verification/{dataset_id}", None),
                ("POST", f"/api/data-verification/{dataset_id}/quote", {
                    "d6_description": {
                        "domain_class": "education_learning",
                        "record_granularity": "entity",
                        "temporal_scope": "current_snapshot",
                        "update_cadence": "one_time",
                        "intended_use_tags": [],
                        "known_limitation_tags": [],
                    },
                    "preview_requested": True,
                }),
                ("POST", f"/api/data-verification/{dataset_id}/start", {
                    "accept_quote": True,
                    "publication_terms_ack": True,
                    "corpus_ack": True,
                }),
                ("POST", f"/api/data-verification/{dataset_id}/publish", {"confirmed": True}),
            )
            for method, path, body in calls:
                response = await client.request(method, path, json=body)
                assert response.status_code == 200
                assert response.json() == expected
