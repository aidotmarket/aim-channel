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
from app.routers.data_verification import router
from app.schemas.data_verification import LifecycleCommand, QuoteProbeRequest, ScanSpecIssueRequest
from app.services.data_verification_client import DataVerificationClient
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
            })
        return httpx.Response(200, json={
            "verification_id": VERIFICATION_ID,
            "state": "CAPTURED",
            "authorization_usd": "25.00",
            "captured_usd": "1.00",
            "result_available": True,
            "publication_allowed": True,
            "reconciliation_required": False,
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
        await client.ingest_report(json.loads((FIXTURES / "report.json").read_text()))
        await client.status(VERIFICATION_ID)
        for action in ("cancel", "publish", "decline", "withdraw"):
            await client.command(LifecycleCommand(
                verification_id=VERIFICATION_ID,
                listing_id=LISTING_ID,
                source_handle_id="dataset_fixture",
                requested_action=action,
            ))

    expected = contract["routes"]
    assert seen == [
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
async def test_local_router_is_disabled_by_default_and_confirmation_is_never_prechecked(monkeypatch):
    monkeypatch.setenv("DATA_VERIFICATION_ENABLED", "false")
    SQLModel.metadata.create_all(get_engine())
    dataset_id = "router-s1590-fixture"
    with get_session_context() as session:
        if session.get(DatasetRecord, dataset_id) is None:
            session.add(DatasetRecord(
                id=dataset_id,
                original_filename="source.csv",
                storage_filename="source.csv",
                file_type="csv",
                status="preview_ready",
                listing_id=LISTING_ID,
            ))
            session.commit()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="seller", key_id="test", scopes=["read", "write"], valid=True
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/data-verification/{dataset_id}")
        assert response.status_code == 200
        assert response.json()["supported"] is False
        assert "not enabled" in response.json()["unavailable_reason"]
        response = await client.post(f"/api/data-verification/{dataset_id}/publish", json={"confirmed": False})
        assert response.status_code == 422
