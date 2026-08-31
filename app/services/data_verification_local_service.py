"""Resumable local orchestration for the seller-facing verification flow."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import or_, update
from sqlmodel import select

from app.core.database import get_session_context
from app.models.data_verification import DataVerificationRun
from app.models.dataset import DatasetRecord
from app.schemas.data_verification import (
    DataVerificationView,
    D6Description,
    LifecycleCommand,
    PaymentLifecycleStatus,
    PrepareVerificationRequest,
    QuoteProbeRequest,
    QuoteProbeView,
    QuoteResponse,
    ReportIngestResponse,
    ScanSpecIssueRequest,
    StartVerificationRequest,
)
from app.services.data_verification.scanner import (
    DataVerificationScanner,
    ScanRefusedError,
    terminal_receipt_signature_binding,
)
from app.services.data_verification_client import DataVerificationClient
from app.services.data_verification.connectors.eolymp_v1 import (
    probe_object_count,
    validate_artifact_schema,
)
from app.services.marketplace_action_signer import canonical_json_bytes, sign_receipt_payload
from app.services.source_artifact_resolver import resolve_source_artifact


SUPPORTED_SUFFIXES = (".csv", ".tsv", ".json", ".jsonl", ".parquet", ".zip")
TERMINAL_STATES = {
    "PUBLISHED", "DECLINED", "WITHDRAWN", "SUPERSEDED", "AUTH_FAILED",
    "CANCELLED_VOIDED", "FAILED_VOIDED", "CAPTURE_FAILED",
}
CANCEL_STATES = {
    "AUTHORIZING", "AUTHORIZED", "SCANNING_LOCAL", "NARRATING_CLOUD",
    "CAPTURE_PENDING", "CAPTURE_RECONCILING",
}
START_LEASE_SECONDS = 30.0
START_LEASE_HEARTBEAT_SECONDS = 5.0
START_LEASE_POLL_SECONDS = 0.01


class DataVerificationLocalError(RuntimeError):
    """A bounded local refusal that never includes source error text."""


def data_verification_enabled() -> bool:
    value = os.environ.get(
        "DATA_VERIFICATION_ENABLED",
        os.environ.get("AIM_DATA_DATA_VERIFICATION_ENABLED", "false"),
    )
    return value.lower() in {"1", "true", "yes", "on"}


def _dumps(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _loads(value: str | None) -> Any:
    return json.loads(value) if value else None


def _latest_run(dataset_id: str) -> DataVerificationRun | None:
    with get_session_context() as session:
        return session.exec(
            select(DataVerificationRun)
            .where(DataVerificationRun.dataset_id == dataset_id)
            .order_by(DataVerificationRun.created_at.desc())
        ).first()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _active_publication(dataset_id: str) -> dict[str, Any] | None:
    with get_session_context() as session:
        runs = session.exec(
            select(DataVerificationRun)
            .where(DataVerificationRun.dataset_id == dataset_id)
            .order_by(DataVerificationRun.created_at.desc())
        ).all()
    for run in runs:
        if run.state == "WITHDRAWN":
            if run.withdrawn_at_utc and _now_utc() - _utc(run.withdrawn_at_utc) <= timedelta(days=30):
                return {
                    "publication_state": "WITHDRAWN",
                    "verification_id": run.verification_id,
                    "withdrawn_at_utc": _utc(run.withdrawn_at_utc).isoformat().replace("+00:00", "Z"),
                }
            return None
        if run.state == "PUBLISHED" and run.report_json:
            report = _loads(run.report_json)
            payment = _loads(run.payment_status_json) or {}
            return {
                "publication_state": "PUBLISHED",
                "verification_id": run.verification_id,
                "scan_date": report.get("completed_at_utc"),
                "report": report,
                "report_ingest": _loads(run.report_ingest_json),
                "d8_preview": _loads(run.d8_json),
                "captured_usd": payment.get("captured_usd"),
            }
    return None


def _view(dataset: DatasetRecord, run: DataVerificationRun | None) -> DataVerificationView:
    supported = dataset.status == "preview_ready" and dataset.original_filename.lower().endswith(SUPPORTED_SUFFIXES)
    unavailable = None
    if not data_verification_enabled():
        return DataVerificationView(
            dataset_id=dataset.id,
            supported=False,
            unavailable_reason="Data verification is not enabled on this AIM Data installation.",
        )
    elif dataset.status != "preview_ready":
        unavailable = "Finish processing this dataset before starting data verification."
    elif not dataset.original_filename.lower().endswith(SUPPORTED_SUFFIXES):
        unavailable = "This source is not supported by the eolymp verification connector."
    elif not dataset.listing_id:
        unavailable = "Save this dataset as an ai.market listing before starting data verification."

    status_data = _loads(run.payment_status_json) if run else None
    payment_status = PaymentLifecycleStatus.model_validate(status_data) if status_data else None
    may_reveal = bool(payment_status and payment_status.result_available)
    return DataVerificationView(
        dataset_id=dataset.id,
        supported=supported and bool(dataset.listing_id),
        unavailable_reason=unavailable,
        run_id=run.id if run else None,
        listing_id=dataset.listing_id,
        state=(payment_status.state if payment_status else run.state) if run else None,
        d6_description=D6Description.model_validate_json(run.d6_json) if run else None,
        preview_requested=run.preview_requested if run else False,
        quote_probe=(QuoteProbeView.model_validate_json(run.probe_json) if run else None),
        quote=QuoteResponse.model_validate_json(run.quote_json) if run and run.quote_json else None,
        payment_status=payment_status,
        report_ingest=(ReportIngestResponse.model_validate_json(run.report_ingest_json) if run and run.report_ingest_json else None),
        findings=_loads(run.report_json) if run and may_reveal else None,
        d8_preview=_loads(run.d8_json) if run and may_reveal else None,
        active_publication=_active_publication(dataset.id),
    )


def get_view(dataset_id: str) -> DataVerificationView:
    with get_session_context() as session:
        dataset = session.get(DatasetRecord, dataset_id)
    if dataset is None:
        raise DataVerificationLocalError("dataset was not found")
    return _view(dataset, _latest_run(dataset_id))


def requires_cloud_refresh(dataset_id: str) -> bool:
    """Return whether the current run has a server identity to reconcile."""
    run = _latest_run(dataset_id)
    return bool(run and run.verification_id)


def _probe(
    dataset: DatasetRecord, preview_requested: bool
) -> tuple[QuoteProbeRequest, QuoteProbeView]:
    if not dataset.listing_id:
        raise DataVerificationLocalError("listing registration is required")
    try:
        listing_id = UUID(dataset.listing_id)
    except ValueError as exc:
        raise DataVerificationLocalError("listing registration is invalid") from exc
    try:
        artifact = resolve_source_artifact(dataset.listing_id)
    except Exception as exc:
        raise DataVerificationLocalError("registered listing source is unavailable") from exc
    if artifact is None:
        raise DataVerificationLocalError("registered listing source is unavailable")
    if artifact.kind == "local" and artifact.local_path:
        try:
            path = Path(artifact.local_path)
            payload = path.read_bytes()
            size_bytes = len(payload)
            objects_discovered = probe_object_count(path.name, payload)
            validate_artifact_schema(path.name, payload)
        except OSError as exc:
            raise DataVerificationLocalError("registered listing source is unavailable") from exc
        except ValueError as exc:
            raise DataVerificationLocalError("registered listing source is not fully supported") from exc
    elif artifact.metadata is not None:
        size_bytes = int(artifact.metadata.size_bytes or 0)
        objects_discovered = artifact.resolved_object_count()
    else:
        raise DataVerificationLocalError("registered listing source is unavailable")
    size_class = "small" if size_bytes < 10_000_000 else "medium" if size_bytes < 100_000_000 else "large"
    try:
        metadata = json.loads(dataset.metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    columns = int(metadata.get("column_count") or len(metadata.get("columns") or []) or 1)
    estimated_tokens = max(1, ceil((1024 + columns * 640) / 3))
    probe = QuoteProbeRequest(
        listing_id=listing_id,
        source_handle_id=dataset.id,
        connector_type="eolymp",
        connector_version="eolymp-v1",
        owner_consent=True,
        source_reachable=True,
        objects_discovered=objects_discovered,
        size_class=size_class,
        supported_capabilities=(
            "complete_traversal",
            "deterministic_object_order",
            "fixed_bucket_aggregates",
            "exact_or_declared_estimated_row_counts",
        ),
        estimated_max_input_tokens=estimated_tokens,
        preview_requested=preview_requested,
    )
    view = QuoteProbeView(
        source_reachable=probe.source_reachable,
        objects_discovered=probe.objects_discovered,
        fixed_reason_skips={},
        size_class=probe.size_class,
    )
    return probe, view


async def prepare_quote(
    dataset_id: str,
    request: PrepareVerificationRequest,
    *,
    client: DataVerificationClient,
) -> DataVerificationView:
    if not data_verification_enabled():
        raise DataVerificationLocalError("data verification is disabled")
    with get_session_context() as session:
        dataset = session.get(DatasetRecord, dataset_id)
    if dataset is None:
        raise DataVerificationLocalError("dataset was not found")
    base_view = _view(dataset, _latest_run(dataset_id))
    if not base_view.supported:
        raise DataVerificationLocalError(base_view.unavailable_reason or "data verification is unavailable")

    latest = _latest_run(dataset_id)
    d6_json = _dumps(request.d6_description.model_dump(mode="json"))
    if latest and latest.state == "QUOTED" and latest.d6_json == d6_json and latest.preview_requested == request.preview_requested:
        return _view(dataset, latest)
    if latest and latest.state not in TERMINAL_STATES | {"QUOTED"}:
        raise DataVerificationLocalError("the current verification run must finish before another can start")

    probe, probe_view = _probe(dataset, request.preview_requested)
    quote = await client.quote(probe)
    if latest and latest.state == "QUOTED":
        latest = _save_run(
            latest.id,
            accepted_at_utc=None,
            preview_requested=request.preview_requested,
            publication_terms_ack=False,
            corpus_ack=False,
            d6_json=d6_json,
            probe_json=_dumps(probe_view.model_dump(mode="json")),
            quote_json=_dumps(quote.model_dump(mode="json")),
        )
        return _view(dataset, latest)
    run = DataVerificationRun(
        dataset_id=dataset.id,
        listing_id=str(dataset.listing_id),
        source_handle_id=dataset.id,
        state="QUOTED",
        idempotency_key=f"idem_{uuid4().hex}",
        owner_authorization_id=f"auth_{uuid4().hex}",
        accepted_at_utc=None,
        preview_requested=request.preview_requested,
        publication_terms_ack=False,
        corpus_ack=False,
        d6_json=d6_json,
        probe_json=_dumps(probe_view.model_dump(mode="json")),
        quote_json=_dumps(quote.model_dump(mode="json")),
    )
    with get_session_context() as session:
        session.add(run)
        session.commit()
        session.refresh(run)
    return _view(dataset, run)


def _save_run(run_id: str, **changes: Any) -> DataVerificationRun:
    with get_session_context() as session:
        run = session.get(DataVerificationRun, run_id)
        if run is None:
            raise DataVerificationLocalError("verification run was not found")
        for field, value in changes.items():
            setattr(run, field, value)
        run.updated_at = datetime.now(timezone.utc)
        session.add(run)
        session.commit()
        session.refresh(run)
        return run


def _claim(run_id: str, field: str) -> bool:
    if field not in {"start_claimed", "scan_claimed"}:
        raise ValueError("unsupported verification claim")
    column = getattr(DataVerificationRun, field)
    with get_session_context() as session:
        result = session.exec(
            update(DataVerificationRun)
            .where(DataVerificationRun.id == run_id, column.is_(False))
            .values({field: True, "updated_at": datetime.now(timezone.utc)})
        )
        session.commit()
        return result.rowcount == 1


def _try_claim_start_lease(run_id: str, owner_id: str) -> bool:
    now = _now_utc()
    with get_session_context() as session:
        result = session.exec(
            update(DataVerificationRun)
            .where(
                DataVerificationRun.id == run_id,
                DataVerificationRun.report_ingest_json.is_(None),
                or_(
                    DataVerificationRun.start_lease_owner_id.is_(None),
                    DataVerificationRun.start_lease_expires_at_utc.is_(None),
                    DataVerificationRun.start_lease_expires_at_utc <= now,
                    DataVerificationRun.start_lease_owner_id == owner_id,
                ),
            )
            .values(
                start_lease_owner_id=owner_id,
                start_lease_expires_at_utc=now
                + timedelta(seconds=START_LEASE_SECONDS),
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1


def _renew_start_lease(run_id: str, owner_id: str) -> bool:
    now = _now_utc()
    with get_session_context() as session:
        result = session.exec(
            update(DataVerificationRun)
            .where(
                DataVerificationRun.id == run_id,
                DataVerificationRun.start_lease_owner_id == owner_id,
            )
            .values(
                start_lease_expires_at_utc=now
                + timedelta(seconds=START_LEASE_SECONDS),
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1


def _release_start_lease(run_id: str, owner_id: str) -> None:
    with get_session_context() as session:
        session.exec(
            update(DataVerificationRun)
            .where(
                DataVerificationRun.id == run_id,
                DataVerificationRun.start_lease_owner_id == owner_id,
            )
            .values(
                start_lease_owner_id=None,
                start_lease_expires_at_utc=None,
                updated_at=_now_utc(),
            )
        )
        session.commit()


async def _claim_start_lease_or_wait(
    run_id: str, owner_id: str
) -> tuple[DataVerificationRun, bool]:
    while True:
        with get_session_context() as session:
            run = session.get(DataVerificationRun, run_id)
        if run is None:
            raise DataVerificationLocalError("verification run was not found")
        lease_is_live = bool(
            run.start_lease_owner_id
            and run.start_lease_expires_at_utc
            and _utc(run.start_lease_expires_at_utc) > _now_utc()
        )
        if run.report_ingest_json and not lease_is_live:
            return run, False
        if _try_claim_start_lease(run_id, owner_id):
            with get_session_context() as session:
                claimed = session.get(DataVerificationRun, run_id)
            if claimed is None:
                raise DataVerificationLocalError("verification run was not found")
            return claimed, True
        await asyncio.sleep(START_LEASE_POLL_SECONDS)


def _assert_start_lease(run_id: str, owner_id: str) -> None:
    if not _renew_start_lease(run_id, owner_id):
        raise DataVerificationLocalError("verification start lease was lost")


@asynccontextmanager
async def _heartbeat_start_lease(run_id: str, owner_id: str):
    stopped = asyncio.Event()
    lost = asyncio.Event()

    async def heartbeat() -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stopped.wait(), timeout=START_LEASE_HEARTBEAT_SECONDS
                )
                return
            except TimeoutError:
                if not _renew_start_lease(run_id, owner_id):
                    lost.set()
                    return

    task = asyncio.create_task(heartbeat())
    try:
        yield lost
    finally:
        stopped.set()
        await task
        _release_start_lease(run_id, owner_id)


def _assert_heartbeat_lease(
    run_id: str, owner_id: str, lost: asyncio.Event
) -> None:
    if lost.is_set():
        raise DataVerificationLocalError("verification start lease was lost")
    _assert_start_lease(run_id, owner_id)


async def _sync_status(
    run: DataVerificationRun,
    client: DataVerificationClient,
    *,
    lease_owner_id: str | None = None,
    lease_lost: asyncio.Event | None = None,
) -> DataVerificationRun:
    if not run.verification_id:
        return run
    status = await client.status(run.verification_id)
    if lease_owner_id and lease_lost:
        _assert_heartbeat_lease(run.id, lease_owner_id, lease_lost)
    changes: dict[str, Any] = {
        "state": status.state,
        "payment_status_json": _dumps(status.model_dump(mode="json")),
    }
    if status.state == "WITHDRAWN":
        withdrawn_at = (
            status.withdrawn_at_utc
            or run.withdrawn_at_utc
            or run.withdraw_requested_at_utc
        )
        if withdrawn_at is not None:
            changes["withdrawn_at_utc"] = withdrawn_at
    return _save_run(run.id, **changes)


async def _ingest_persisted_report(
    run: DataVerificationRun,
    client: DataVerificationClient,
    *,
    lease_owner_id: str,
    lease_lost: asyncio.Event,
) -> DataVerificationRun:
    report = _loads(run.report_json)
    if report is None:
        raise DataVerificationLocalError("the persisted verification report is unavailable")
    ingest = await client.ingest_report(report)
    _assert_heartbeat_lease(run.id, lease_owner_id, lease_lost)
    run = _save_run(
        run.id, report_ingest_json=_dumps(ingest.model_dump(mode="json"))
    )
    return await _sync_status(
        run,
        client,
        lease_owner_id=lease_owner_id,
        lease_lost=lease_lost,
    )


async def refresh(dataset_id: str, *, client: DataVerificationClient) -> DataVerificationView:
    if not data_verification_enabled():
        raise DataVerificationLocalError("data verification is disabled")
    with get_session_context() as session:
        dataset = session.get(DatasetRecord, dataset_id)
    if dataset is None:
        raise DataVerificationLocalError("dataset was not found")
    run = _latest_run(dataset_id)
    if run and run.verification_id:
        run = await _sync_status(run, client)
    return _view(dataset, run)


def _terminal_report(
    signed_spec: dict[str, Any],
    *,
    install_id: str,
    install_private_key: Any,
) -> dict[str, Any]:
    payload = signed_spec["payload"]
    report: dict[str, Any] = {
        "verification_id": payload["verification_id"],
        "listing_id": payload["listing_id"],
        "source_handle_id": payload["source_handle_id"],
        "spec_id": payload["spec_id"],
        "spec_version": payload["spec_version"],
        "spec_hash": signed_spec["spec_hash"],
        "nonce_echo": payload["nonce"],
        "install_key_id": install_id,
        "agent_version": "aim-data-verification-v1",
        "connector_type": payload["connector_type"],
        "connector_version": payload["connector_version"],
        "terminal_error_code": "scanner_failure",
        "completed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "canonicalization_version": "python-json-sort-compact-v1",
        "signature_algorithm": "Ed25519",
    }
    report["receipt_signature"] = sign_receipt_payload(
        terminal_receipt_signature_binding(report), install_private_key
    )
    return report


async def start(
    dataset_id: str,
    *,
    request: StartVerificationRequest,
    client: DataVerificationClient,
    scanner: DataVerificationScanner,
    install_id: str,
    install_private_key: Any,
) -> DataVerificationView:
    if not data_verification_enabled():
        raise DataVerificationLocalError("data verification is disabled")
    with get_session_context() as session:
        dataset = session.get(DatasetRecord, dataset_id)
    run = _latest_run(dataset_id)
    if dataset is None or run is None or not run.quote_json:
        raise DataVerificationLocalError("a current verification quote is required")
    if not run.publication_terms_ack or not run.corpus_ack:
        run = _save_run(
            run.id,
            accepted_at_utc=datetime.now(timezone.utc),
            publication_terms_ack=request.publication_terms_ack,
            corpus_ack=request.corpus_ack,
        )

    lease_owner_id = str(uuid4())
    run, lease_acquired = await _claim_start_lease_or_wait(
        run.id, lease_owner_id
    )
    if not lease_acquired:
        if run.verification_id:
            run = await _sync_status(run, client)
        return _view(dataset, run)
    async with _heartbeat_start_lease(run.id, lease_owner_id) as lease_lost:
        return await _start_with_lease(
            dataset,
            run,
            client=client,
            scanner=scanner,
            install_id=install_id,
            install_private_key=install_private_key,
            lease_owner_id=lease_owner_id,
            lease_lost=lease_lost,
        )


async def _start_with_lease(
    dataset: DatasetRecord,
    run: DataVerificationRun,
    *,
    client: DataVerificationClient,
    scanner: DataVerificationScanner,
    install_id: str,
    install_private_key: Any,
    lease_owner_id: str,
    lease_lost: asyncio.Event,
) -> DataVerificationView:
    if run.report_ingest_json:
        return _view(dataset, run)
    if run.report_json:
        run = await _ingest_persisted_report(
            run,
            client,
            lease_owner_id=lease_owner_id,
            lease_lost=lease_lost,
        )
        return _view(dataset, run)

    quote = QuoteResponse.model_validate_json(run.quote_json)
    accepted_at = run.accepted_at_utc
    if accepted_at is None:
        raise DataVerificationLocalError("the quote acknowledgements were not accepted")
    if accepted_at.tzinfo is None:
        accepted_at = accepted_at.replace(tzinfo=timezone.utc)
    issue = ScanSpecIssueRequest(
        listing_id=UUID(run.listing_id),
        source_handle_id=run.source_handle_id,
        owner_authorization_id=run.owner_authorization_id,
        quote_id=quote.quote_id,
        idempotency_key=run.idempotency_key,
        accepted_at_utc=accepted_at,
        connector_type="eolymp",
        connector_version="eolymp-v1",
        depth_class=quote.depth_class,
        preview_requested=run.preview_requested,
        wire_manifest_version="data-verification-wire-v1",
        corpus_disclosure_version="s1396-disclosure-v1",
        payment_disclosure_version="payment-disclosure-v1",
        authorization_usd=quote.hard_maximum.authorization_usd,
    )
    recover_scan_claim = run.scan_claimed
    if not run.start_claimed and not _claim(run.id, "start_claimed"):
        raise DataVerificationLocalError("the server start claim could not be acquired")
    spec = await client.start(issue)
    _assert_heartbeat_lease(run.id, lease_owner_id, lease_lost)
    if run.verification_id and run.verification_id != spec.payload.verification_id:
        raise DataVerificationLocalError(
            "the idempotent server start returned a different verification identity"
        )
    spec_dict = spec.model_dump(mode="json")
    run = _save_run(run.id, verification_id=spec.payload.verification_id)
    run = await _sync_status(
        run,
        client,
        lease_owner_id=lease_owner_id,
        lease_lost=lease_lost,
    )
    if run.state != "AUTHORIZED":
        return _view(dataset, run)

    if not recover_scan_claim and not _claim(run.id, "scan_claimed"):
        raise DataVerificationLocalError("the local scan claim could not be acquired")

    try:
        execution = await asyncio.to_thread(
            scanner.scan,
            signed_spec=spec_dict,
            d6_candidate=json.loads(run.d6_json),
        )
        report = execution.report
        d8 = execution.d8_projection
    except (ScanRefusedError, OSError, ValueError, RuntimeError):
        report = _terminal_report(
            spec_dict,
            install_id=install_id,
            install_private_key=install_private_key,
        )
        d8 = None
    _assert_heartbeat_lease(run.id, lease_owner_id, lease_lost)
    run = _save_run(
        run.id,
        report_json=_dumps(report),
        d8_json=_dumps(d8) if d8 is not None else None,
    )
    run = await _ingest_persisted_report(
        run,
        client,
        lease_owner_id=lease_owner_id,
        lease_lost=lease_lost,
    )
    return _view(dataset, run)


async def lifecycle_command(
    dataset_id: str,
    action: str,
    *,
    client: DataVerificationClient,
) -> DataVerificationView:
    if not data_verification_enabled():
        raise DataVerificationLocalError("data verification is disabled")
    with get_session_context() as session:
        dataset = session.get(DatasetRecord, dataset_id)
    run = _latest_run(dataset_id)
    if dataset is None or run is None or not run.verification_id or not run.payment_status_json:
        raise DataVerificationLocalError("an active verification run is required")
    status = PaymentLifecycleStatus.model_validate_json(run.payment_status_json)
    if action == "cancel" and status.state not in CANCEL_STATES:
        raise DataVerificationLocalError("cancel is not available in the current server state")
    if action == "publish" and (status.state != "CAPTURED" or not status.publication_allowed):
        raise DataVerificationLocalError("publication is not available in the current server state")
    if action == "publish":
        ingest = (
            ReportIngestResponse.model_validate_json(run.report_ingest_json)
            if run.report_ingest_json
            else None
        )
        grounded_text_ready = bool(
            ingest
            and ingest.narrative_state == "grounded"
            and ingest.narrative
            and ingest.narrative.strip()
            and ingest.listing_claim_comparison
            and ingest.listing_claim_comparison.strip()
        )
        fingerprint_notice_ready = bool(
            ingest and ingest.narrative_state == "withheld_grounding_failed"
        )
        if not (grounded_text_ready or fingerprint_notice_ready):
            raise DataVerificationLocalError(
                "the full allAI interpretation text is not available for publication review"
            )
    if action == "decline" and status.state != "CAPTURED":
        raise DataVerificationLocalError("decline is not available in the current server state")
    if action == "withdraw" and status.state != "PUBLISHED":
        raise DataVerificationLocalError("withdrawal is not available in the current server state")
    command = LifecycleCommand(
        verification_id=run.verification_id,
        listing_id=UUID(run.listing_id),
        source_handle_id=run.source_handle_id,
        requested_action=action,
    )
    if action == "withdraw" and run.withdraw_requested_at_utc is None:
        run = _save_run(
            run.id, withdraw_requested_at_utc=_now_utc()
        )
    result = await client.command(command)
    updated = result.status
    changes: dict[str, Any] = {
        "state": updated.state,
        "payment_status_json": _dumps(updated.model_dump(mode="json")),
    }
    if action == "withdraw":
        withdrawn_at = (
            updated.withdrawn_at_utc
            or result.server_date_utc
            or run.withdraw_requested_at_utc
        )
        if withdrawn_at is not None:
            changes["withdrawn_at_utc"] = withdrawn_at
    run = _save_run(
        run.id,
        **changes,
    )
    return _view(dataset, run)
