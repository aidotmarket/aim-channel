"""Authenticated local API for the AIM Data verification seller flow."""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.api_key_auth import AuthenticatedUser, get_current_user
from app.config import settings
from app.core.crypto import DeviceCrypto
from app.schemas.data_verification import (
    ConfirmLifecycleRequest,
    DataVerificationView,
    PrepareVerificationRequest,
    StartVerificationRequest,
)
from app.services.data_verification.scanner import DataVerificationScanner
from app.services.data_verification_client import DataVerificationClient, DataVerificationClientError
from app.services.data_verification_local_service import (
    DataVerificationLocalError,
    data_verification_enabled,
    get_view,
    lifecycle_command,
    prepare_quote,
    refresh,
    requires_cloud_refresh,
    start,
)
from app.services.registration_service import ensure_vz_install_registered
from app.services.serial_store import get_serial_store


router = APIRouter(prefix="/data-verification")


@dataclass(frozen=True)
class VerificationRuntime:
    client: DataVerificationClient
    scanner: DataVerificationScanner
    install_id: str
    install_private_key: object


def _platform_public_key() -> bytes:
    configured = os.environ.get("DATA_VERIFICATION_PLATFORM_PUBLIC_KEY_PEM", "").strip()
    if not configured:
        raise DataVerificationLocalError("platform verification key is not configured")
    if configured.startswith("-----BEGIN PUBLIC KEY-----"):
        return configured.replace("\\n", "\n").encode("ascii")
    try:
        decoded = base64.b64decode(configured, validate=True)
    except ValueError as exc:
        raise DataVerificationLocalError("platform verification key is invalid") from exc
    if not decoded.startswith(b"-----BEGIN PUBLIC KEY-----"):
        raise DataVerificationLocalError("platform verification key is invalid")
    return decoded


def _commitment_key() -> bytes:
    path = Path(settings.data_directory) / ".data_verification_commitment_key"
    try:
        if path.exists():
            key = base64.b64decode(path.read_bytes(), validate=True)
        else:
            key = secrets.token_bytes(32)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64encode(key))
            path.chmod(0o600)
    except (OSError, ValueError) as exc:
        raise DataVerificationLocalError("customer-held commitment key is unavailable") from exc
    if len(key) != 32:
        raise DataVerificationLocalError("customer-held commitment key is invalid")
    return key


async def build_runtime(request: Request, user: AuthenticatedUser) -> VerificationRuntime:
    if not data_verification_enabled():
        raise DataVerificationLocalError("data verification is disabled")
    if not settings.keystore_passphrase:
        raise DataVerificationLocalError("AIM Data signing is not configured")
    crypto = DeviceCrypto(keystore_path=settings.keystore_path, passphrase=settings.keystore_passphrase)
    install_private_key, _install_public_key, _x_private, _x_public = crypto.get_or_create_keypairs()
    store = get_serial_store()
    incoming = request.headers.get("Authorization", "")
    incoming_token = incoming.removeprefix("Bearer ").strip() if incoming.startswith("Bearer ") else None
    seller_access_token = store.state.ai_market_access_token or incoming_token
    seller_id = (
        store.state.ai_market_seller_id
        or (user.user_id if user.key_id == "ai_market_bearer" else None)
        or (store.state.last_status_cache or {}).get("gateway_user_id")
    )
    if not seller_access_token or not seller_id:
        raise DataVerificationLocalError("sign in with ai.market before data verification")
    install_id = await ensure_vz_install_registered(
        crypto,
        access_token=seller_access_token,
        seller_id=str(seller_id),
    )
    if not install_id:
        raise DataVerificationLocalError("AIM Data install registration is unavailable")
    client = DataVerificationClient(
        base_url=settings.ai_market_url,
        seller_id=str(seller_id),
        install_id=install_id,
        install_private_key=install_private_key,
        seller_access_token=seller_access_token,
    )
    scanner = DataVerificationScanner(
        commitment_key=_commitment_key(),
        install_private_key=install_private_key,
        install_key_id=install_id,
        platform_public_key=_platform_public_key(),
    )
    return VerificationRuntime(client, scanner, install_id, install_private_key)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DataVerificationClientError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/{dataset_id}", response_model=DataVerificationView)
async def verification_view(
    dataset_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DataVerificationView:
    try:
        view = get_view(dataset_id)
        if not view.supported or not view.run_id or not requires_cloud_refresh(dataset_id):
            return view
        runtime = await build_runtime(request, user)
        return await refresh(dataset_id, client=runtime.client)
    except (DataVerificationLocalError, DataVerificationClientError) as exc:
        raise _http_error(exc) from exc


@router.post("/{dataset_id}/quote", response_model=DataVerificationView)
async def quote_verification(
    dataset_id: str,
    body: PrepareVerificationRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DataVerificationView:
    try:
        runtime = await build_runtime(request, user)
        return await prepare_quote(dataset_id, body, client=runtime.client)
    except (DataVerificationLocalError, DataVerificationClientError) as exc:
        raise _http_error(exc) from exc


@router.post("/{dataset_id}/start", response_model=DataVerificationView)
async def start_verification(
    dataset_id: str,
    _body: StartVerificationRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DataVerificationView:
    try:
        runtime = await build_runtime(request, user)
        return await start(
            dataset_id,
            client=runtime.client,
            scanner=runtime.scanner,
            install_id=runtime.install_id,
            install_private_key=runtime.install_private_key,
        )
    except (DataVerificationLocalError, DataVerificationClientError) as exc:
        raise _http_error(exc) from exc


@router.post("/{dataset_id}/{action}", response_model=DataVerificationView)
async def run_lifecycle_command(
    dataset_id: str,
    action: Literal["cancel", "publish", "decline", "withdraw"],
    _body: ConfirmLifecycleRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DataVerificationView:
    try:
        runtime = await build_runtime(request, user)
        return await lifecycle_command(dataset_id, action, client=runtime.client)
    except (DataVerificationLocalError, DataVerificationClientError) as exc:
        raise _http_error(exc) from exc
