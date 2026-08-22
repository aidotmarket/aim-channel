import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlmodel import SQLModel, select

from app.core.database import get_engine, get_session_context
from app.models.data_verification import DataVerificationRun
from app.models.dataset import DatasetRecord
from app.schemas.data_verification import (
    D6Description,
    PaymentLifecycleStatus,
    PrepareVerificationRequest,
    QuoteResponse,
    ReportIngestResponse,
    StartVerificationRequest,
)
from app.services import data_verification_local_service as local_service
from app.services import source_artifact_resolver as resolver
from app.services.data_verification.contract import SignedScanSpec
from app.services.data_verification.scanner import ScanExecution
from app.services.data_verification_local_service import (
    DataVerificationLocalError,
    get_view,
    lifecycle_command,
    prepare_quote,
    refresh,
    start,
)
from app.services.data_verification_client import LifecycleCommandResult


FIXTURES = Path(__file__).parent / "fixtures" / "data_verification_v1"
LISTING_ID = "11111111-1111-4111-8111-111111111111"
VERIFICATION_ID = "22222222-2222-4222-8222-222222222222"
VALID_D6 = D6Description(
    domain_class="education_learning",
    record_granularity="entity",
    temporal_scope="current_snapshot",
    update_cadence="one_time",
    intended_use_tags=("analysis_reporting",),
    known_limitation_tags=(),
)


class FakeScanner:
    def __init__(self, dataset_id: str):
        self.dataset_id = dataset_id
        self.calls = 0

    def scan(self, **kwargs):
        self.calls += 1
        verification_id = kwargs["signed_spec"]["payload"]["verification_id"]
        report = json.loads((FIXTURES / "report.json").read_text())
        report.update(
            verification_id=verification_id,
            listing_id=LISTING_ID,
            source_handle_id=self.dataset_id,
            install_key_id="install_fixture",
        )
        return ScanExecution(report=report, d8_projection=[{"row_count": 12, "row_count_method": "exact"}])


class RefusingScanner:
    def scan(self, **_kwargs):
        from app.services.data_verification.scanner import ScanRefusedError
        raise ScanRefusedError("private/source.csv could not be decoded")


class FakeClient:
    def __init__(self, final_state: str = "CAPTURED"):
        self.final_state = final_state
        self.quote_calls = 0
        self.start_requests = 0
        self.start_calls = 0
        self.ingest_calls = 0
        self.status_calls = 0
        self.commands = []
        self.verification_id = VERIFICATION_ID
        self.last_report = None
        self.last_probe = None
        self._issued_specs = {}

    async def quote(self, probe):
        self.quote_calls += 1
        self.last_probe = probe
        return QuoteResponse.model_validate({
            "quote_id": "quote_fixture",
            "depth_class": "complete_standard_v1",
            "traversal_scope": "all_reachable_supported_objects",
            "row_count_policy": "exact_or_declared_estimate",
            "low_occupancy_behavior": "suppressed_low_occupancy",
            "minimum_aggregate_occupancy": 10,
            "hard_maximum": {
                "authorization_usd": "25.00",
                "inference": {"max_input_tokens": 8192, "max_output_tokens": 1024, "model_request_count": 1},
            },
            "partial_traversal_allowed": False,
        })

    async def start(self, request):
        self.start_requests += 1
        if request.idempotency_key in self._issued_specs:
            return self._issued_specs[request.idempotency_key]
        self.start_calls += 1
        self.verification_id = str(
            uuid5(NAMESPACE_URL, f"{request.source_handle_id}:{request.idempotency_key}")
        )
        document = json.loads((FIXTURES / "scan_spec.json").read_text())
        issued_at = request.accepted_at_utc + timedelta(seconds=1)
        document["payload"].update(
            listing_id=LISTING_ID,
            source_handle_id=request.source_handle_id,
            owner_authorization_id=request.owner_authorization_id,
            quote_id=request.quote_id,
            idempotency_key=request.idempotency_key,
            accepted_at_utc=request.accepted_at_utc.isoformat(),
            issued_at_utc=issued_at.isoformat(),
            expires_at_utc=(issued_at + timedelta(minutes=10)).isoformat(),
            verification_id=self.verification_id,
        )
        issued = SignedScanSpec.model_validate(document)
        self._issued_specs[request.idempotency_key] = issued
        return issued

    async def ingest_report(self, _report):
        self.ingest_calls += 1
        self.last_report = _report
        return ReportIngestResponse(
            verification_id=self.verification_id,
            accepted=True,
            narrative_state="grounded",
            narrative="Grounded allAI narrative fixture.",
            listing_claim_comparison="Listing claims match the deterministic scan fixture.",
        )

    async def status(self, _verification_id):
        self.status_calls += 1
        state = "AUTHORIZED" if self.ingest_calls == 0 else self.final_state
        return PaymentLifecycleStatus(
            verification_id=self.verification_id,
            state=state,
            authorization_usd="25.00",
            captured_usd="1.23" if state in {"CAPTURED", "PUBLISHED", "DECLINED"} else None,
            result_available=state in {"CAPTURED", "PUBLISHED"},
            publication_allowed=state == "CAPTURED",
            reconciliation_required=state == "CAPTURE_RECONCILING",
        )

    async def command(self, command):
        self.commands.append(command)
        state = {"publish": "PUBLISHED", "decline": "DECLINED", "withdraw": "WITHDRAWN", "cancel": "CANCELLED_VOIDED"}[command.requested_action]
        return LifecycleCommandResult(
            status=PaymentLifecycleStatus(
                verification_id=self.verification_id,
                state=state,
                authorization_usd="25.00",
                captured_usd="1.23" if command.requested_action != "cancel" else None,
                result_available=state == "PUBLISHED",
                publication_allowed=False,
                reconciliation_required=False,
            ),
            server_date_utc=datetime(2026, 8, 22, 18, 30, tzinfo=timezone.utc),
        )


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("DATA_VERIFICATION_ENABLED", "true")
    SQLModel.metadata.create_all(get_engine())


def make_dataset(tmp_path, monkeypatch, *, suffix="csv"):
    dataset_id = f"ds-{tmp_path.name}"[-36:]
    uploads = tmp_path / "uploads"
    processed = tmp_path / "processed"
    uploads.mkdir()
    processed.mkdir()
    monkeypatch.setattr(resolver.settings, "upload_directory", str(uploads))
    monkeypatch.setattr(resolver.settings, "processed_directory", str(processed))
    filename = f"source.{suffix}"
    (uploads / filename).write_text("id,name\n1,alpha\n")
    with get_session_context() as session:
        session.add(DatasetRecord(
            id=dataset_id,
            original_filename=filename,
            storage_filename=filename,
            file_type=suffix,
            status="preview_ready",
            listing_id=LISTING_ID,
            metadata_json=json.dumps({"column_count": 2}),
        ))
        session.commit()
    return dataset_id


def prepare_body():
    return PrepareVerificationRequest(
        d6_description=VALID_D6,
        preview_requested=True,
    )


def start_body():
    return StartVerificationRequest(
        accept_quote=True,
        publication_terms_ack=True,
        corpus_ack=True,
    )


@pytest.mark.asyncio
async def test_quote_start_capture_resume_and_publish_are_idempotent(tmp_path, monkeypatch):
    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = FakeClient()
    scanner = FakeScanner(dataset_id)

    quoted = await prepare_quote(dataset_id, prepare_body(), client=client)
    assert quoted.state == "QUOTED"
    assert quoted.quote.hard_maximum.authorization_usd == 25
    captured = await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=scanner,
        install_id="install_fixture",
        install_private_key=object(),
    )
    assert captured.state == "CAPTURED"
    assert captured.findings is not None
    assert captured.d8_preview is not None
    assert (client.start_calls, client.ingest_calls, scanner.calls) == (1, 1, 1)

    duplicate = await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=scanner,
        install_id="install_fixture",
        install_private_key=object(),
    )
    assert duplicate.payment_status.verification_id == captured.payment_status.verification_id
    assert (client.start_calls, client.ingest_calls, scanner.calls) == (1, 1, 1)

    resumed = await refresh(dataset_id, client=client)
    assert resumed.state == "CAPTURED"
    assert (client.start_calls, client.ingest_calls, scanner.calls) == (1, 1, 1)
    published = await lifecycle_command(dataset_id, "publish", client=client)
    assert published.state == "PUBLISHED"
    assert client.commands[-1].requested_action == "publish"


@pytest.mark.asyncio
async def test_reconciliation_hides_local_findings_and_blocks_publication(tmp_path, monkeypatch):
    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = FakeClient(final_state="CAPTURE_RECONCILING")
    await prepare_quote(dataset_id, prepare_body(), client=client)
    view = await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=FakeScanner(dataset_id),
        install_id="install_fixture",
        install_private_key=object(),
    )
    assert view.payment_status.reconciliation_required is True
    assert view.findings is None
    with pytest.raises(DataVerificationLocalError, match="publication is not available"):
        await lifecycle_command(dataset_id, "publish", client=client)


@pytest.mark.asyncio
async def test_rerun_creates_fresh_acknowledgements_and_preserves_active_publication(tmp_path, monkeypatch):
    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = FakeClient()
    await prepare_quote(dataset_id, prepare_body(), client=client)
    await start(dataset_id, request=start_body(), client=client, scanner=FakeScanner(dataset_id), install_id="install_fixture", install_private_key=object())
    await lifecycle_command(dataset_id, "publish", client=client)

    rerun = await prepare_quote(dataset_id, prepare_body(), client=client)
    assert rerun.state == "QUOTED"
    assert rerun.active_publication is not None
    assert rerun.active_publication["publication_state"] == "PUBLISHED"
    assert rerun.active_publication["report"] == client.last_report
    assert rerun.active_publication["d8_preview"] == [{"row_count": 12, "row_count_method": "exact"}]
    assert rerun.active_publication["captured_usd"] == "1.23"
    with get_session_context() as session:
        runs = session.exec(select(DataVerificationRun).where(DataVerificationRun.dataset_id == dataset_id)).all()
    assert len(runs) == 2
    assert runs[0].publication_terms_ack and runs[0].corpus_ack
    assert not runs[1].publication_terms_ack and not runs[1].corpus_ack
    assert runs[1].accepted_at_utc is None


def test_feature_flag_and_unsupported_connector_fail_closed(tmp_path, monkeypatch):
    dataset_id = make_dataset(tmp_path, monkeypatch, suffix="pdf")
    view = get_view(dataset_id)
    assert view.supported is False
    assert "not supported" in view.unavailable_reason
    monkeypatch.setenv("DATA_VERIFICATION_ENABLED", "false")
    view = get_view(dataset_id)
    assert view.supported is False
    assert "not enabled" in view.unavailable_reason


@pytest.mark.asyncio
async def test_feature_flag_blocks_cloud_refresh_and_lifecycle_commands(tmp_path, monkeypatch):
    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = FakeClient()
    await prepare_quote(dataset_id, prepare_body(), client=client)
    monkeypatch.setenv("DATA_VERIFICATION_ENABLED", "false")
    with pytest.raises(DataVerificationLocalError, match="disabled"):
        await refresh(dataset_id, client=client)
    with pytest.raises(DataVerificationLocalError, match="disabled"):
        await lifecycle_command(dataset_id, "publish", client=client)


@pytest.mark.asyncio
async def test_local_failure_sends_only_bounded_terminal_report_and_hides_results(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = FakeClient(final_state="FAILED_VOIDED")
    await prepare_quote(dataset_id, prepare_body(), client=client)
    view = await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=RefusingScanner(),
        install_id="install_fixture",
        install_private_key=Ed25519PrivateKey.generate(),
    )
    assert view.state == "FAILED_VOIDED"
    assert view.findings is None
    assert client.last_report["terminal_error_code"] == "scanner_failure"
    transmitted = json.dumps(client.last_report)
    assert "private/source.csv" not in transmitted
    assert "could not be decoded" not in transmitted


@pytest.mark.asyncio
async def test_concurrent_starts_share_one_epoch_and_one_scanner_execution(tmp_path, monkeypatch):
    class DelayedClient(FakeClient):
        async def start(self, request):
            await asyncio.sleep(0)
            return await super().start(request)

        async def status(self, verification_id):
            await asyncio.sleep(0)
            return await super().status(verification_id)

        async def ingest_report(self, report):
            await asyncio.sleep(0)
            return await super().ingest_report(report)

    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = DelayedClient()
    scanner = FakeScanner(dataset_id)
    await prepare_quote(dataset_id, prepare_body(), client=client)

    first, second = await asyncio.gather(*(
        start(
            dataset_id,
            request=start_body(),
            client=client,
            scanner=scanner,
            install_id="install_fixture",
            install_private_key=object(),
        )
        for _ in range(2)
    ))

    assert first.payment_status.verification_id == second.payment_status.verification_id
    assert first.state == second.state == "CAPTURED"
    assert client.start_calls == 1
    assert scanner.calls == 1
    assert client.ingest_calls == 1


@pytest.mark.asyncio
async def test_retry_after_report_persistence_gap_ingests_without_rescanning(tmp_path, monkeypatch):
    class GapClient(FakeClient):
        async def ingest_report(self, report):
            self.ingest_calls += 1
            self.last_report = report
            if self.ingest_calls == 1:
                raise RuntimeError("simulated ingest persistence gap")
            return ReportIngestResponse(
                verification_id=self.verification_id,
                accepted=True,
                narrative_state="grounded",
                narrative="Grounded allAI narrative fixture.",
                listing_claim_comparison="Listing claims match the deterministic scan fixture.",
            )

    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = GapClient()
    scanner = FakeScanner(dataset_id)
    await prepare_quote(dataset_id, prepare_body(), client=client)

    with pytest.raises(RuntimeError, match="persistence gap"):
        await start(
            dataset_id,
            request=start_body(),
            client=client,
            scanner=scanner,
            install_id="install_fixture",
            install_private_key=object(),
        )
    recovered = await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=scanner,
        install_id="install_fixture",
        install_private_key=object(),
    )

    assert recovered.state == "CAPTURED"
    assert scanner.calls == 1
    assert client.start_calls == 1
    assert client.ingest_calls == 2


@pytest.mark.asyncio
async def test_quote_precedes_stored_acceptance_and_zip_probe_is_honest(tmp_path, monkeypatch):
    dataset_id = make_dataset(tmp_path, monkeypatch, suffix="zip")
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("a.csv", "id\n1\n")
        archive.writestr("b.csv", "id\n2\n")
    (tmp_path / "uploads" / "source.zip").write_bytes(archive_bytes.getvalue())
    monkeypatch.setattr(
        zipfile.ZipFile,
        "read",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the free zip probe must not decompress entries")
        ),
    )
    client = FakeClient()

    quoted = await prepare_quote(dataset_id, prepare_body(), client=client)
    assert quoted.quote_probe.objects_discovered == 2
    assert client.last_probe.objects_discovered == 2
    with get_session_context() as session:
        run = session.exec(
            select(DataVerificationRun).where(DataVerificationRun.dataset_id == dataset_id)
        ).one()
        assert run.accepted_at_utc is None
        assert run.publication_terms_ack is False
        assert run.corpus_ack is False


@pytest.mark.asyncio
async def test_published_to_withdrawn_persists_server_authoritative_marker_date(tmp_path, monkeypatch):
    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = FakeClient()
    await prepare_quote(dataset_id, prepare_body(), client=client)
    await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=FakeScanner(dataset_id),
        install_id="install_fixture",
        install_private_key=object(),
    )
    await lifecycle_command(dataset_id, "publish", client=client)
    withdrawn = await lifecycle_command(dataset_id, "withdraw", client=client)

    assert withdrawn.state == "WITHDRAWN"
    assert withdrawn.active_publication == {
        "publication_state": "WITHDRAWN",
        "verification_id": client.verification_id,
        "withdrawn_at_utc": "2026-08-22T18:30:00Z",
    }


@pytest.mark.asyncio
async def test_grounded_result_without_display_text_cannot_publish_but_can_decline(
    tmp_path, monkeypatch
):
    class MissingNarrativeClient(FakeClient):
        async def ingest_report(self, report):
            self.ingest_calls += 1
            self.last_report = report
            return ReportIngestResponse(
                verification_id=self.verification_id,
                accepted=True,
                narrative_state="grounded",
            )

    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = MissingNarrativeClient()
    await prepare_quote(dataset_id, prepare_body(), client=client)
    await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=FakeScanner(dataset_id),
        install_id="install_fixture",
        install_private_key=object(),
    )

    with pytest.raises(DataVerificationLocalError, match="full allAI interpretation text"):
        await lifecycle_command(dataset_id, "publish", client=client)
    declined = await lifecycle_command(dataset_id, "decline", client=client)
    assert declined.state == "DECLINED"


@pytest.mark.asyncio
async def test_withdraw_without_response_date_uses_precommand_request_time(
    tmp_path, monkeypatch
):
    class MissingDateClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.persisted_request_at = None

        async def command(self, command):
            result = await super().command(command)
            if command.requested_action == "withdraw":
                with get_session_context() as session:
                    run = session.exec(
                        select(DataVerificationRun).where(
                            DataVerificationRun.dataset_id == dataset_id
                        )
                    ).one()
                    self.persisted_request_at = run.withdraw_requested_at_utc
                return LifecycleCommandResult(status=result.status, server_date_utc=None)
            return result

    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = MissingDateClient()
    await prepare_quote(dataset_id, prepare_body(), client=client)
    await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=FakeScanner(dataset_id),
        install_id="install_fixture",
        install_private_key=object(),
    )
    await lifecycle_command(dataset_id, "publish", client=client)
    withdrawn = await lifecycle_command(dataset_id, "withdraw", client=client)

    requested_at = client.persisted_request_at.replace(tzinfo=timezone.utc)
    assert withdrawn.active_publication["withdrawn_at_utc"] == (
        requested_at.isoformat().replace("+00:00", "Z")
    )
    monkeypatch.setattr(
        local_service, "_now_utc", lambda: requested_at + timedelta(days=29)
    )
    assert get_view(dataset_id).active_publication["withdrawn_at_utc"] == (
        requested_at.isoformat().replace("+00:00", "Z")
    )


@pytest.mark.asyncio
async def test_lost_withdraw_response_refresh_recovers_original_request_time(
    tmp_path, monkeypatch
):
    class LostResponseClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.withdraw_processed = False
            self.persisted_request_at = None

        async def command(self, command):
            if command.requested_action != "withdraw":
                return await super().command(command)
            with get_session_context() as session:
                run = session.exec(
                    select(DataVerificationRun).where(
                        DataVerificationRun.dataset_id == dataset_id
                    )
                ).one()
                self.persisted_request_at = run.withdraw_requested_at_utc
            self.withdraw_processed = True
            raise RuntimeError("simulated lost withdrawal response")

        async def status(self, verification_id):
            if not self.withdraw_processed:
                return await super().status(verification_id)
            return PaymentLifecycleStatus(
                verification_id=self.verification_id,
                state="WITHDRAWN",
                authorization_usd="25.00",
                captured_usd="1.23",
                result_available=False,
                publication_allowed=False,
                reconciliation_required=False,
            )

    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = LostResponseClient()
    await prepare_quote(dataset_id, prepare_body(), client=client)
    await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=FakeScanner(dataset_id),
        install_id="install_fixture",
        install_private_key=object(),
    )
    await lifecycle_command(dataset_id, "publish", client=client)
    with pytest.raises(RuntimeError, match="lost withdrawal response"):
        await lifecycle_command(dataset_id, "withdraw", client=client)

    refreshed = await refresh(dataset_id, client=client)
    requested_at = client.persisted_request_at.replace(tzinfo=timezone.utc)
    assert refreshed.state == "WITHDRAWN"
    assert refreshed.active_publication["withdrawn_at_utc"] == (
        requested_at.isoformat().replace("+00:00", "Z")
    )
    monkeypatch.setattr(
        local_service, "_now_utc", lambda: requested_at + timedelta(days=29)
    )
    assert get_view(dataset_id).active_publication["withdrawn_at_utc"] == (
        requested_at.isoformat().replace("+00:00", "Z")
    )


@pytest.mark.asyncio
async def test_withdraw_status_body_timestamp_is_preferred_over_transport_date(
    tmp_path, monkeypatch
):
    class BodyTimestampClient(FakeClient):
        async def command(self, command):
            result = await super().command(command)
            if command.requested_action == "withdraw":
                return LifecycleCommandResult(
                    status=result.status.model_copy(
                        update={
                            "withdrawn_at_utc": datetime(
                                2026, 8, 21, 7, 15, tzinfo=timezone.utc
                            )
                        }
                    ),
                    server_date_utc=datetime(
                        2026, 8, 22, 18, 30, tzinfo=timezone.utc
                    ),
                )
            return result

    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = BodyTimestampClient()
    await prepare_quote(dataset_id, prepare_body(), client=client)
    await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=FakeScanner(dataset_id),
        install_id="install_fixture",
        install_private_key=object(),
    )
    await lifecycle_command(dataset_id, "publish", client=client)
    withdrawn = await lifecycle_command(dataset_id, "withdraw", client=client)
    assert withdrawn.active_publication["withdrawn_at_utc"] == "2026-08-21T07:15:00Z"


def _accepted_recovery_run(
    dataset_id: str,
    *,
    verification_id: str | None,
    start_claimed: bool,
    scan_claimed: bool,
) -> DataVerificationRun:
    with get_session_context() as session:
        run = session.exec(
            select(DataVerificationRun).where(DataVerificationRun.dataset_id == dataset_id)
        ).one()
        run.accepted_at_utc = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
        run.publication_terms_ack = True
        run.corpus_ack = True
        run.verification_id = verification_id
        run.start_claimed = start_claimed
        run.scan_claimed = scan_claimed
        session.add(run)
        session.commit()
        session.refresh(run)
        return run


@pytest.mark.asyncio
async def test_restart_reexecutes_deterministic_scan_for_persisted_claims(
    tmp_path, monkeypatch
):
    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = FakeClient()
    scanner = FakeScanner(dataset_id)
    await prepare_quote(dataset_id, prepare_body(), client=client)
    with get_session_context() as session:
        quoted = session.exec(
            select(DataVerificationRun).where(
                DataVerificationRun.dataset_id == dataset_id
            )
        ).one()
        expected_id = str(
            uuid5(NAMESPACE_URL, f"{dataset_id}:{quoted.idempotency_key}")
        )
    _accepted_recovery_run(
        dataset_id,
        verification_id=expected_id,
        start_claimed=True,
        scan_claimed=True,
    )
    monkeypatch.setattr(local_service, "CLAIM_WAIT_ATTEMPTS", 1)

    recovered = await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=scanner,
        install_id="install_fixture",
        install_private_key=object(),
    )

    with get_session_context() as session:
        runs = session.exec(
            select(DataVerificationRun).where(
                DataVerificationRun.dataset_id == dataset_id
            )
        ).all()
    expected_report = json.loads((FIXTURES / "report.json").read_text())
    expected_report.update(
        verification_id=expected_id,
        listing_id=LISTING_ID,
        source_handle_id=dataset_id,
        install_key_id="install_fixture",
    )
    assert recovered.state == "CAPTURED"
    assert len(runs) == 1
    assert runs[0].verification_id == expected_id
    assert (client.start_calls, client.ingest_calls, scanner.calls) == (1, 1, 1)
    assert client.last_report == expected_report


@pytest.mark.asyncio
async def test_restart_recovers_start_claim_without_verification_identity(
    tmp_path, monkeypatch
):
    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = FakeClient()
    scanner = FakeScanner(dataset_id)
    await prepare_quote(dataset_id, prepare_body(), client=client)
    _accepted_recovery_run(
        dataset_id,
        verification_id=None,
        start_claimed=True,
        scan_claimed=False,
    )
    monkeypatch.setattr(local_service, "CLAIM_WAIT_ATTEMPTS", 1)

    recovered = await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=scanner,
        install_id="install_fixture",
        install_private_key=object(),
    )

    assert recovered.state == "CAPTURED"
    assert (client.start_calls, client.ingest_calls, scanner.calls) == (1, 1, 1)
    assert recovered.payment_status.verification_id == client.verification_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminated_claim", "expected_start_requests"),
    [("start_claimed", 1), ("scan_claimed", 2)],
)
async def test_restart_after_each_claim_compare_and_set_completes_once(
    tmp_path, monkeypatch, terminated_claim, expected_start_requests
):
    class SimulatedTermination(BaseException):
        pass

    dataset_id = make_dataset(tmp_path, monkeypatch)
    client = FakeClient()
    scanner = FakeScanner(dataset_id)
    await prepare_quote(dataset_id, prepare_body(), client=client)
    original_claim = local_service._claim
    terminated = False

    def terminate_after_claim(run_id, field):
        nonlocal terminated
        claimed = original_claim(run_id, field)
        if field == terminated_claim and claimed and not terminated:
            terminated = True
            raise SimulatedTermination()
        return claimed

    monkeypatch.setattr(local_service, "_claim", terminate_after_claim)
    with pytest.raises(SimulatedTermination):
        await start(
            dataset_id,
            request=start_body(),
            client=client,
            scanner=scanner,
            install_id="install_fixture",
            install_private_key=object(),
        )
    monkeypatch.setattr(local_service, "_claim", original_claim)
    monkeypatch.setattr(local_service, "CLAIM_WAIT_ATTEMPTS", 1)

    recovered = await start(
        dataset_id,
        request=start_body(),
        client=client,
        scanner=scanner,
        install_id="install_fixture",
        install_private_key=object(),
    )

    with get_session_context() as session:
        runs = session.exec(
            select(DataVerificationRun).where(
                DataVerificationRun.dataset_id == dataset_id
            )
        ).all()
    assert recovered.state == "CAPTURED"
    assert len(runs) == 1
    assert runs[0].verification_id == client.verification_id
    assert client.start_calls == 1
    assert client.start_requests == expected_start_requests
    assert scanner.calls == 1
    assert client.ingest_calls == 1
