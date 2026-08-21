"""
Marketplace Publish Router
==========================

BQ-VZ-PUBLISH Phase 3: Proxies listing publish requests from VZ frontend
to ai.market backend, signing with Ed25519 JWT.

Flow: VZ Frontend -> VZ Backend (this router) -> ai.market Backend
The Ed25519 private key lives on VZ backend only.
"""

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.api_key_auth import get_current_user
from app.config import settings
from app.core.channel_config import CHANNEL
from app.core.crypto import DeviceCrypto
from app.core.database import get_session_context
from app.models.s3_connection import S3Connection
from app.models.s3_object_metadata import S3ObjectMetadata
from app.models.s3_scan_job import S3ScanJob
from app.services.registration_service import ensure_vz_install_registered
from app.services.listing_versioning import build_version_prefix
from app.services.marketplace_action_signer import (
    build_action_jwt,
    canonical_json_bytes,
    canonical_payload_hash,
)
from app.services.processing_service import ProcessingService, get_processing_service
from app.services.s3_publish_source_resolver import (
    NotS3PublishSource,
    S3PublishSourceResolution,
    S3PublishSourceResolutionError,
    resolve_s3_publish_source,
)
from app.services.serial_store import get_serial_store
from sqlmodel import select

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class MarketplacePublishRequest(BaseModel):
    title: str = Field(..., max_length=200)
    description: str = Field(..., max_length=5000)
    tags: list[str] = Field(default_factory=list)
    category: Optional[str] = None
    pricing_type: Literal["one_time", "subscription"] = "one_time"
    price_cents: int = Field(..., ge=0)
    row_count: Optional[int] = None
    column_names: Optional[list[str]] = None
    column_types: Optional[list[str]] = None
    file_format: Optional[str] = None
    file_size_bytes: Optional[int] = None
    schema_info: Optional[dict[str, Any]] = None
    compliance_details: Optional[dict[str, Any]] = None
    compliance_status: Optional[str] = None
    privacy_score: Optional[float] = Field(None, ge=0, le=10)
    secondary_categories: Optional[list[str]] = None
    model_provider: Optional[str] = None
    vz_dataset_id: str  # local VZ dataset ID, becomes vz_raw_listing_id


class MarketplacePublishResponse(BaseModel):
    status: str
    listing_id: Optional[str] = None
    marketplace_url: Optional[str] = None
    error: Optional[str] = None


class DisclosureApprovedSample(BaseModel):
    columns: list[str]
    row_refs: list[str]
    rows: list[dict[str, Any]]


class DisclosureSnapshotProxyRequest(BaseModel):
    dataset_id: str = Field(..., min_length=1)
    approved_fields: dict[str, Any]
    sample_decision: Literal["none", "approved_rows"]
    approved_sample: Optional[DisclosureApprovedSample] = None
    ai_training_notification_ack: bool
    ai_training_notification_text: str = Field(..., min_length=1)
    license: str = Field(..., min_length=1)
    approval_source: Literal["aim_channel"]
    source_publish_operation_id: str = Field(..., min_length=1)


class DisclosureSnapshotProxyResponse(BaseModel):
    status: str
    listing_id: str
    disclosure_version: Optional[str] = None


class VersionPublishEmit(BaseModel):
    """Strict allowlist for the receiver's VersionPublish contract."""

    model_config = {"extra": "forbid"}

    version_label: str = Field(..., min_length=1, max_length=64)
    object_count: int = Field(..., ge=0)
    total_size_bytes: int = Field(..., ge=0)
    manifest_hash: str = Field(..., min_length=1, max_length=256)


class S3ConnectionPublishEmit(BaseModel):
    """Strict allowlist for the receiver's S3ConnectionPublish contract."""

    model_config = {"extra": "forbid"}

    bucket: str
    region: str
    role_arn: str
    prefix: str
    serial_id: str


class MarketplaceVersionPublishRequest(MarketplacePublishRequest):
    s3_connection_id: str
    scan_job_id: str
    version_label: str = Field(..., min_length=1, max_length=64)


class MarketplaceVersionPublishResponse(MarketplacePublishResponse):
    version_label: str
    version_status: Optional[str] = None
    quarantine_reason: Optional[str] = None


class MarketplaceVersionConfirmResponse(BaseModel):
    version_id: str
    listing_id: str
    version_label: str
    status: str
    quarantine_reason: Optional[str] = None
    result: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VZ_SERIAL_RE = re.compile(r"\bVZ-[A-Za-z0-9][A-Za-z0-9_-]{6,}\b")
HMAC_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
ATTESTATION_HASH_RE = re.compile(r"^(?:[a-fA-F0-9]{64}|[a-fA-F0-9]{128})$")
ATTESTATION_HASH_PATHS = {
    ("schema_info", "attestation", "data_hash"),
    ("schema_info", "attestation", "attestation_hash"),
}


def _jcs_canonical_bytes(body: dict) -> bytes:
    """RFC 8785 JCS-style canonical bytes used by sender/backend parity tests."""
    return canonical_json_bytes(body)


def _jcs_hash(body: dict) -> str:
    """RFC 8785 JCS-style canonical hash (sorted keys, compact separators)."""
    return canonical_payload_hash(body)


def _build_jwt(seller_id: str, install_id: str, metadata_hash: str, ed_priv) -> str:
    """Create a short-lived EdDSA JWT for the publish action."""
    return build_action_jwt(
        seller_id=seller_id,
        install_id=install_id,
        action="publish_listing",
        payload_hash=metadata_hash,
        private_key=ed_priv,
        hash_claim="metadata_hash",
    )


def _get_crypto() -> DeviceCrypto:
    """Get an initialized DeviceCrypto instance."""
    if not settings.keystore_passphrase:
        raise HTTPException(
            status_code=503,
            detail="Keystore passphrase not configured — cannot sign marketplace requests",
        )
    return DeviceCrypto(
        keystore_path=settings.keystore_path,
        passphrase=settings.keystore_passphrase,
    )


def _build_s3_connection_emit(resolution: S3PublishSourceResolution) -> dict[str, str]:
    """Build the exact allowlisted s3_connection block sent to ai.market."""
    return S3ConnectionPublishEmit(
        bucket=resolution.bucket,
        region=resolution.region,
        role_arn=resolution.role_arn,
        prefix=resolution.prefix,
        serial_id=str(resolution.serial_id),
    ).model_dump(mode="json")


def _build_version_emit(version: VersionPublishEmit) -> dict[str, Any]:
    return version.model_dump(mode="json")


def _build_publish_payload(
    body: MarketplacePublishRequest,
    s3_source: NotS3PublishSource | S3PublishSourceResolution,
    versions: Optional[list[VersionPublishEmit]] = None,
) -> dict[str, Any]:
    """Build the single canonical publish payload used for wire body and hash."""
    payload = body.model_dump(exclude_none=True)
    payload["vz_raw_listing_id"] = payload.pop("vz_dataset_id")
    payload["download_channel"] = CHANNEL.value
    if isinstance(s3_source, S3PublishSourceResolution):
        payload["s3_connection"] = _build_s3_connection_emit(s3_source)
    if versions:
        payload["versions"] = [_build_version_emit(version) for version in versions]
    return payload


def _manifest_hash_for_scan_rows(rows: list[S3ObjectMetadata]) -> str:
    """Scan-snapshot digest including scan-time last_modified/etag metadata.

    Receivers treat this as an opaque immutability key for the version label,
    not as a digest of current live S3 state.
    """
    manifest = [
        {
            "etag": row.etag,
            "key": row.object_key,
            "last_modified": row.last_modified.isoformat(),
            "size": row.size_bytes,
        }
        for row in rows
    ]
    return _jcs_hash({"objects": manifest})


def _manifest_hash_for_scan_job(session, scan_job: S3ScanJob) -> str:
    rows = session.exec(
        select(S3ObjectMetadata)
        .where(S3ObjectMetadata.scan_job_id == scan_job.id)
        .order_by(S3ObjectMetadata.object_key)
    ).all()
    return _manifest_hash_for_scan_rows(rows)


def _version_emit_from_scan(
    *,
    connection_id: str,
    scan_job_id: str,
    version_label: str,
    user,
) -> VersionPublishEmit:
    with get_session_context() as session:
        connection = session.get(S3Connection, connection_id)
        if connection is None:
            raise HTTPException(status_code=404, detail="S3 connection not found")
        if connection.owner_id != user.user_id:
            raise HTTPException(status_code=403, detail="S3 connection is not owned by this user")
        try:
            version_prefix = build_version_prefix(str(connection.prefix or ""), version_label)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"invalid version prefix: {exc}") from exc

        scan_job = session.get(S3ScanJob, scan_job_id)
        if scan_job is None or scan_job.connection_id != connection_id:
            raise HTTPException(status_code=404, detail="S3 version scan job not found")
        if scan_job.status != "completed":
            raise HTTPException(status_code=409, detail="S3 version scan must complete before publish")
        rows = session.exec(
            select(S3ObjectMetadata)
            .where(S3ObjectMetadata.scan_job_id == scan_job.id)
            .order_by(S3ObjectMetadata.object_key)
        ).all()
        if not rows:
            raise HTTPException(status_code=409, detail="S3 version scan found no objects under the version prefix")
        if any(not row.object_key.startswith(version_prefix) for row in rows):
            raise HTTPException(status_code=409, detail="S3 version scan does not match the requested version prefix")

        return VersionPublishEmit(
            version_label=version_label,
            object_count=len(rows),
            total_size_bytes=sum(row.size_bytes for row in rows),
            manifest_hash=_manifest_hash_for_scan_rows(rows),
        )


def _assert_no_sensitive_publish_values(payload: dict[str, Any]) -> None:
    """Reject accidental serial/hash material in seller-controlled publish fields."""

    def _check_string(candidate: str) -> None:
        if VZ_SERIAL_RE.search(candidate) or HMAC_HEX_RE.search(candidate):
            raise HTTPException(status_code=409, detail="Publish payload contains sensitive material")

    def _check_attestation_hash(value: Any) -> None:
        if not isinstance(value, str) or not ATTESTATION_HASH_RE.fullmatch(value):
            raise HTTPException(status_code=409, detail="Publish payload contains invalid attestation hash")

    def _walk(value: Any, path: tuple[str, ...]) -> None:
        if path in ATTESTATION_HASH_PATHS:
            _check_attestation_hash(value)
            return
        if isinstance(value, str):
            _check_string(value)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                _walk(child, (*path, str(key)))
            return
        if isinstance(value, list):
            for child in value:
                _walk(child, path)

    for key in (
        "title",
        "description",
        "category",
        "vz_raw_listing_id",
        "tags",
        "schema_info",
        "compliance_details",
        "compliance_status",
        "secondary_categories",
        "model_provider",
    ):
        if key in payload:
            _walk(payload[key], (key,))


def _seller_auth_headers(request: Request) -> dict[str, str]:
    store = get_serial_store()
    incoming_auth = request.headers.get("Authorization", "")
    if incoming_auth.startswith("Bearer "):
        return {"Authorization": incoming_auth}
    if store.state.ai_market_access_token:
        return {"Authorization": f"Bearer {store.state.ai_market_access_token}"}
    incoming_api_key = request.headers.get("X-API-Key")
    if incoming_api_key:
        return {"X-API-Key": incoming_api_key}
    return {}


def _payload_hash(value: Any) -> str:
    return hashlib.sha256(_jcs_canonical_bytes(value)).hexdigest()


def _persist_disclosure_decision(
    processing: ProcessingService,
    record,
    *,
    status: Literal["draft", "publish_pending", "snapshot_pending", "complete", "failed"],
    listing_id: str,
    body: DisclosureSnapshotProxyRequest,
    disclosure_version: Optional[str] = None,
    last_error: Optional[str] = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    approved_sample = body.approved_sample.model_dump() if body.approved_sample else None
    existing = record.metadata.get("disclosure_decision")
    created_at = existing.get("created_at") if isinstance(existing, dict) else None
    decision = {
        "status": status,
        "source_publish_operation_id": body.source_publish_operation_id,
        "listing_id": listing_id,
        "disclosure_version": disclosure_version,
        "approved_fields_hash": _payload_hash(body.approved_fields),
        "sample_decision": body.sample_decision,
        "approved_sample_hash": _payload_hash(approved_sample) if approved_sample else None,
        "approved_sample_row_count": len(approved_sample["rows"]) if approved_sample else 0,
        "approved_sample_columns": approved_sample["columns"] if approved_sample else [],
        "ai_training_notification_text": body.ai_training_notification_text,
        "license": body.license,
        "created_at": created_at or now,
        "updated_at": now,
        "last_error": last_error,
    }
    if status == "snapshot_pending" and approved_sample:
        decision["approved_sample_replay"] = approved_sample
    if status == "snapshot_pending":
        decision["approved_payload_replay"] = body.model_dump(exclude={"dataset_id"}, exclude_none=False)
    record.metadata["disclosure_decision"] = decision
    record.listing_id = listing_id
    storage_fn = record.upload_path.name if record.upload_path else record.id
    processing._save_record(record, storage_fn)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/marketplace/publish", response_model=MarketplacePublishResponse)
async def publish_to_marketplace(
    body: MarketplacePublishRequest,
    request: Request,
    user=Depends(get_current_user),
    processing: ProcessingService = Depends(get_processing_service),
):
    """Publish a dataset listing to ai.market via signed JWT proxy."""
    data = await publish_via_signed_proxy(body, request, user)
    listing_id = data.get("listing_id")
    if listing_id and hasattr(processing, "get_dataset"):
        record = processing.get_dataset(body.vz_dataset_id)
        if record:
            record.listing_id = str(listing_id)
            storage_fn = record.upload_path.name if record.upload_path else record.id
            processing._save_record(record, storage_fn)
    return MarketplacePublishResponse(
        status="published",
        listing_id=listing_id,
        marketplace_url=data.get("marketplace_url"),
    )


@router.post(
    "/marketplace/listings/{listing_id}/disclosure-snapshots",
    response_model=DisclosureSnapshotProxyResponse,
)
async def create_disclosure_snapshot(
    listing_id: str,
    body: DisclosureSnapshotProxyRequest,
    request: Request,
    user=Depends(get_current_user),
    processing: ProcessingService = Depends(get_processing_service),
):
    """Forward a seller-authorized disclosure snapshot request to ai.market."""
    if not listing_id.strip():
        raise HTTPException(status_code=422, detail="listing_id is required")

    record = processing.get_dataset(body.dataset_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Dataset '{body.dataset_id}' not found")

    payload = body.model_dump(exclude={"dataset_id"}, exclude_none=False)
    if payload["sample_decision"] == "none":
        payload["approved_sample"] = None
    _persist_disclosure_decision(
        processing,
        record,
        status="snapshot_pending",
        listing_id=listing_id,
        body=body,
        last_error=None,
    )

    auth_headers = _seller_auth_headers(request)
    if not auth_headers:
        raise HTTPException(status_code=409, detail="Seller ai.market token or API key not available")

    url = f"{settings.ai_market_url}/api/v1/listings/{listing_id}/disclosure-snapshots"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    **auth_headers,
                    "Content-Type": "application/json",
                },
            )
    except httpx.ConnectError as exc:
        _persist_disclosure_decision(processing, record, status="snapshot_pending", listing_id=listing_id, body=body, last_error=str(exc))
        raise HTTPException(status_code=502, detail="Cannot reach ai.market — disclosure snapshot pending")
    except httpx.TimeoutException as exc:
        _persist_disclosure_decision(processing, record, status="snapshot_pending", listing_id=listing_id, body=body, last_error=str(exc))
        raise HTTPException(status_code=504, detail="ai.market disclosure snapshot timed out")

    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code not in (200, 201):
        detail = data.get("detail") or data.get("error") or resp.text or f"ai.market returned {resp.status_code}"
        _persist_disclosure_decision(processing, record, status="snapshot_pending", listing_id=listing_id, body=body, last_error=str(detail))
        raise HTTPException(status_code=resp.status_code, detail=detail)

    disclosure_version = data.get("disclosure_version")
    try:
        _persist_disclosure_decision(
            processing,
            record,
            status="complete",
            listing_id=listing_id,
            body=body,
            disclosure_version=str(disclosure_version) if disclosure_version else None,
            last_error=None,
        )
    except Exception as exc:
        logger.exception("Disclosure snapshot succeeded but local audit persistence failed")
        raise HTTPException(
            status_code=500,
            detail="Disclosure status unknown: ai.market accepted the snapshot, but AIM Data could not store the local audit record.",
        ) from exc

    return DisclosureSnapshotProxyResponse(
        status="complete",
        listing_id=listing_id,
        disclosure_version=str(disclosure_version) if disclosure_version else None,
    )


@router.post("/marketplace/versions/publish", response_model=MarketplaceVersionPublishResponse)
async def publish_version_to_marketplace(
    body: MarketplaceVersionPublishRequest,
    request: Request,
    user=Depends(get_current_user),
):
    """Publish a new dataset version to ai.market via the signed publish proxy."""
    version = _version_emit_from_scan(
        connection_id=body.s3_connection_id,
        scan_job_id=body.scan_job_id,
        version_label=body.version_label,
        user=user,
    )
    publish_body = MarketplacePublishRequest(
        **body.model_dump(exclude={"s3_connection_id", "scan_job_id", "version_label"})
    )
    data = await publish_via_signed_proxy(publish_body, request, user, versions=[version])
    version_data = data.get("version") if isinstance(data.get("version"), dict) else {}
    versions_data = data.get("versions") if isinstance(data.get("versions"), list) else []
    if not version_data and versions_data:
        version_data = next(
            (item for item in versions_data if item.get("version_label") == body.version_label),
            versions_data[0],
        )
    return MarketplaceVersionPublishResponse(
        status="published",
        listing_id=data.get("listing_id"),
        marketplace_url=data.get("marketplace_url"),
        version_label=body.version_label,
        version_status=version_data.get("status"),
        quarantine_reason=version_data.get("quarantine_reason"),
    )


@router.post("/marketplace/versions/{version_id}/confirm", response_model=MarketplaceVersionConfirmResponse)
async def confirm_marketplace_version(
    version_id: str,
    request: Request,
    user=Depends(get_current_user),
):
    """Approve a receiver-quarantined version for activation."""
    store = get_serial_store()
    incoming_auth = request.headers.get("Authorization", "")
    incoming_bearer = incoming_auth.removeprefix("Bearer ").strip() if incoming_auth.startswith("Bearer ") else None
    seller_token = store.state.ai_market_access_token or incoming_bearer
    if not seller_token:
        raise HTTPException(status_code=409, detail="Seller ai.market token not available")

    url = f"{settings.ai_market_url}/api/v1/vz/versions/{version_id}/confirm"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {seller_token}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Cannot reach ai.market — check network connectivity")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="ai.market request timed out")

    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code not in (200, 201):
        detail = data.get("detail") or data.get("error") or resp.text or f"ai.market returned {resp.status_code}"
        raise HTTPException(status_code=resp.status_code, detail=detail)

    status = str(data.get("status") or "")
    if status == "superseded":
        result = "confirmed_but_superseded"
    else:
        result = "confirmed"
    return MarketplaceVersionConfirmResponse(
        version_id=str(data.get("version_id") or version_id),
        listing_id=str(data.get("listing_id") or ""),
        version_label=str(data.get("version_label") or ""),
        status=status,
        quarantine_reason=data.get("quarantine_reason"),
        result=result,
    )


async def publish_via_signed_proxy(
    body: MarketplacePublishRequest,
    request: Request,
    user,
    versions: Optional[list[VersionPublishEmit]] = None,
    s3_source_override: Optional[S3PublishSourceResolution] = None,
) -> dict[str, Any]:
    """Publish a dataset listing to ai.market via the canonical signed proxy."""
    # 1. Load crypto + keypairs
    crypto = _get_crypto()
    ed_priv, _ed_pub, _x_priv, _x_pub = crypto.get_or_create_keypairs()

    # 2. Resolve install_id (iss) and seller_id (sub)
    store = get_serial_store()
    cached = store.state.last_status_cache or {}
    seller_id = (
        store.state.ai_market_seller_id
        or (user.user_id if getattr(user, "key_id", "") == "ai_market_bearer" else None)
        or cached.get("gateway_user_id")
    )
    if not seller_id:
        raise HTTPException(
            status_code=409,
            detail="Seller identity not available — sign in with ai.market before publishing",
        )

    incoming_auth = request.headers.get("Authorization", "")
    incoming_bearer = incoming_auth.removeprefix("Bearer ").strip() if incoming_auth.startswith("Bearer ") else None
    install_id = await ensure_vz_install_registered(
        crypto,
        access_token=store.state.ai_market_access_token or incoming_bearer,
        seller_id=str(seller_id),
    )
    if not install_id:
        raise HTTPException(
            status_code=409,
            detail="VZ install registration not available — sign in with ai.market and try publishing again",
        )

    # 3. Resolve S3 provenance, then build the canonical payload for ai.market.
    # SECURITY: s3_source_override MUST be a resolution produced by an ownership-validating
    # resolver (e.g. resolve_s3_connection_publish_source, which enforces owner_id, verified
    # status, role_arn, and scope authorization). Callers must never pass an unvalidated
    # override -- doing so would bypass ownership/eligibility checks.
    if s3_source_override is not None:
        if not isinstance(s3_source_override, S3PublishSourceResolution):
            raise HTTPException(status_code=500, detail="Invalid publish source override")
        s3_source: NotS3PublishSource | S3PublishSourceResolution = s3_source_override
    else:
        try:
            s3_source = resolve_s3_publish_source(body.vz_dataset_id, user, store.state)
        except S3PublishSourceResolutionError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    "S3-sourced dataset is not eligible for scoped-credential publish yet; "
                    f"verify the S3 connection and dataset source, then retry. reason={exc.reason}"
                ),
            )

    payload = _build_publish_payload(body, s3_source, versions=versions)
    _assert_no_sensitive_publish_values(payload)

    # 4. JCS hash + JWT
    metadata_hash = _jcs_hash(payload)
    token = _build_jwt(str(seller_id), install_id, metadata_hash, ed_priv)

    # 5. POST to ai.market
    url = f"{settings.ai_market_url}/api/v1/vz/publish"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Cannot reach ai.market — check network connectivity")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="ai.market request timed out")

    # 6. Return response
    if resp.status_code in (200, 201):
        return resp.json()

    # Error passthrough
    try:
        err_data = resp.json()
        detail = err_data.get("detail") or err_data.get("error") or str(err_data)
    except Exception:
        detail = resp.text or f"ai.market returned {resp.status_code}"

    logger.warning("ai.market publish failed (%d): %s", resp.status_code, detail)
    raise HTTPException(status_code=resp.status_code, detail=detail)


@router.get("/marketplace/publish-status")
async def publish_status(user=Depends(get_current_user)):
    """Check if this AIM Data installation is ready to publish to ai.market."""
    # Must have keystore passphrase
    if not settings.keystore_passphrase:
        return {"can_publish": False, "reason": "Keystore passphrase not configured"}

    # Must have keypairs
    try:
        crypto = _get_crypto()
        crypto.get_or_create_keypairs()
    except Exception as e:
        return {"can_publish": False, "reason": f"Keypair error: {e}"}

    # Must have device registration (platform keys)
    if not crypto.has_platform_keys():
        return {"can_publish": False, "reason": "Device not registered with ai.market"}

    return {"can_publish": True, "reason": None}
