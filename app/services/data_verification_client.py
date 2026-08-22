"""Fail-closed signed client for the ai.market data-verification control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.schemas.data_verification import (
    LifecycleCommand,
    PaymentLifecycleStatus,
    QuoteProbeRequest,
    QuoteResponse,
    ReportIngestResponse,
    ScanSpecIssueRequest,
)
from app.services.data_verification.contract import SignedScanSpec
from app.services.marketplace_action_signer import (
    build_action_jwt,
    canonical_json_bytes,
    canonical_payload_hash,
)


class DataVerificationClientError(RuntimeError):
    """A display-safe control-plane failure with no reflected response body."""


@dataclass(frozen=True)
class LifecycleCommandResult:
    status: PaymentLifecycleStatus
    server_date_utc: datetime | None


class DataVerificationClient:
    def __init__(
        self,
        *,
        base_url: str,
        seller_id: str,
        install_id: str,
        install_private_key: Any,
        seller_access_token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._seller_id = seller_id
        self._install_id = install_id
        self._install_private_key = install_private_key
        self._seller_access_token = seller_access_token
        self._http_client = http_client

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            if self._http_client is not None:
                response = await self._http_client.request(method, f"{self._base_url}{path}", **kwargs)
            else:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.request(method, f"{self._base_url}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            raise DataVerificationClientError("ai.market data verification timed out") from exc
        except httpx.RequestError as exc:
            raise DataVerificationClientError("ai.market data verification is unavailable") from exc
        if not response.is_success:
            raise DataVerificationClientError(
                f"ai.market data verification refused the request ({response.status_code})"
            )
        return response

    async def _signed_json(
        self,
        method: str,
        path: str,
        *,
        expected_action: str,
        body: dict[str, Any],
    ) -> httpx.Response:
        token = build_action_jwt(
            seller_id=self._seller_id,
            install_id=self._install_id,
            action=expected_action,
            payload_hash=canonical_payload_hash(body),
            private_key=self._install_private_key,
        )
        return await self._request(
            method,
            path,
            content=canonical_json_bytes(body),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    async def quote(self, probe: QuoteProbeRequest) -> QuoteResponse:
        response = await self._signed_json(
            "POST",
            "/api/v1/data-verification/quote",
            expected_action="data_verification_quote",
            body=probe.model_dump(mode="json"),
        )
        return QuoteResponse.model_validate(response.json())

    async def start(self, request: ScanSpecIssueRequest) -> SignedScanSpec:
        response = await self._signed_json(
            "POST",
            "/api/v1/data-verification/scan-spec",
            expected_action="data_verification_start",
            body=request.model_dump(mode="json"),
        )
        return SignedScanSpec.model_validate(response.json())

    async def ingest_report(self, report: dict[str, Any]) -> ReportIngestResponse:
        response = await self._request(
            "PUT",
            "/api/v1/data-verification/scan-spec",
            content=canonical_json_bytes(report),
            headers={"Content-Type": "application/json"},
        )
        return ReportIngestResponse.model_validate(response.json())

    async def status(self, verification_id: str) -> PaymentLifecycleStatus:
        response = await self._request(
            "GET",
            f"/api/v1/data-verification/{verification_id}/status",
            headers={"Authorization": f"Bearer {self._seller_access_token}"},
        )
        return PaymentLifecycleStatus.model_validate(response.json())

    async def command(self, command: LifecycleCommand) -> LifecycleCommandResult:
        action = command.requested_action
        response = await self._signed_json(
            "POST",
            f"/api/v1/data-verification/{command.verification_id}/{action}",
            expected_action=f"data_verification_{action}",
            body=command.model_dump(mode="json"),
        )
        server_date = response.headers.get("Date")
        observed_at = None
        if server_date:
            try:
                observed_at = parsedate_to_datetime(server_date).astimezone(timezone.utc)
            except (TypeError, ValueError) as exc:
                raise DataVerificationClientError(
                    "ai.market data verification returned an invalid server date"
                ) from exc
        return LifecycleCommandResult(
            status=PaymentLifecycleStatus.model_validate(response.json()),
            server_date_utc=observed_at,
        )
