from contextlib import contextmanager
import json
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session

from app.auth.api_key_auth import AuthenticatedUser, get_current_user
from app.main import app
from app.models.dataset import DatasetRecord  # noqa: F401
from app.models.s3_connection import S3Connection
from app.models.s3_object_metadata import S3ObjectMetadata  # noqa: F401
from app.models.s3_scan_job import S3ScanJob  # noqa: F401
from app.routers import s3_connections
from app.services import s3_object_profiler, s3_scan_service

USER_A = AuthenticatedUser(user_id="user-a", key_id="key-a", scopes=["read", "write"], valid=True)
USER_B = AuthenticatedUser(user_id="user-b", key_id="key-b", scopes=["read", "write"], valid=True)


@pytest.fixture
def s3_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def client(s3_engine, monkeypatch):
    @contextmanager
    def _session_context():
        with Session(s3_engine) as session:
            yield session

    monkeypatch.setattr(s3_connections, "get_session_context", _session_context)
    monkeypatch.setattr(s3_scan_service, "get_session_context", _session_context)
    monkeypatch.setattr(s3_object_profiler, "get_session_context", _session_context)
    monkeypatch.setattr(s3_connections.settings, "ai_market_aws_account_id", "123456789012")
    monkeypatch.setattr(s3_connections.settings, "ai_market_assume_role_principal_arn", None)
    app.dependency_overrides[get_current_user] = lambda: USER_A
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


def _create_connection(client: TestClient) -> dict:
    response = client.post(
        "/api/s3-connections/",
        json={
            "name": "Seller bucket",
            "bucket": "seller-bucket",
            "region": "us-east-1",
            "prefix": "exports/",
        },
    )
    assert response.status_code == 201
    return response.json()


def _configured_row(
    s3_engine,
    *,
    prefix: Optional[str] = "exports/",
    owner_id: Optional[str] = "user-a",
) -> S3Connection:
    connection = S3Connection(
        id=str(uuid4()),
        owner_id=owner_id,
        name="Seller bucket",
        bucket="seller-bucket",
        region="us-east-1",
        prefix=prefix,
        role_arn="arn:aws:iam::210987654321:role/aim-data",
        external_id=str(uuid4()),
        status="configured",
    )
    with Session(s3_engine) as session:
        session.add(connection)
        session.commit()
        session.refresh(connection)
        session.expunge(connection)
    return connection


def _serial_store(*, active: bool = True, serial: str = "VZ-test", install_token: Optional[str] = "vzit-test"):
    return SimpleNamespace(
        state=SimpleNamespace(
            state="active" if active else "unprovisioned",
            serial=serial,
            install_token=install_token,
        )
    )


class DefaultSerialClient:
    async def get_s3_external_id(self, *_args, **_kwargs):
        return {"success": True, "external_id": "external-from-broker"}


@pytest.fixture(autouse=True)
def connected_serial(monkeypatch):
    monkeypatch.setattr(s3_connections, "get_serial_store", lambda: _serial_store())
    monkeypatch.setattr(s3_connections, "SerialClient", DefaultSerialClient)


def test_post_creates_row_and_returns_substituted_policies(client):
    data = _create_connection(client)

    assert data["external_id"]
    assert data["status"] == "onboarding"
    assert data["trust_policy"]["Statement"][0]["Principal"]["AWS"] == "arn:aws:iam::123456789012:root"
    assert data["trust_policy"]["Statement"][0]["Condition"]["StringEquals"]["sts:ExternalId"] == data["external_id"]
    assert data["permission_policy"]["Statement"][0]["Resource"] == "arn:aws:s3:::seller-bucket"
    assert data["permission_policy"]["Statement"][0]["Condition"]["StringLike"]["s3:prefix"] == ["exports/*"]
    assert data["permission_policy"]["Statement"][1]["Resource"] == "arn:aws:s3:::seller-bucket/exports/*"


def test_trust_policy_uses_configured_principal_and_external_id(monkeypatch):
    configured_principal = "arn:aws:iam::123456789012:role/aim-data-assumer"
    s3_connection = S3Connection(
        id=str(uuid4()),
        name="Seller bucket",
        bucket="seller-bucket",
        region="us-east-1",
        prefix=None,
        external_id="external-123",
    )
    monkeypatch.setattr(s3_connections.settings, "ai_market_assume_role_principal_arn", configured_principal)

    policy = s3_connections._trust_policy(s3_connection)
    statement = policy["Statement"][0]

    assert statement["Principal"]["AWS"] == configured_principal
    assert statement["Condition"]["StringEquals"]["sts:ExternalId"] == "external-123"


def test_trust_policy_falls_back_to_account_root_and_external_id(monkeypatch):
    s3_connection = S3Connection(
        id=str(uuid4()),
        name="Seller bucket",
        bucket="seller-bucket",
        region="us-east-1",
        prefix=None,
        external_id="external-456",
    )
    monkeypatch.setattr(s3_connections.settings, "ai_market_assume_role_principal_arn", None)
    monkeypatch.setattr(s3_connections.settings, "ai_market_aws_account_id", "123456789012")

    policy = s3_connections._trust_policy(s3_connection)
    statement = policy["Statement"][0]

    assert statement["Principal"]["AWS"] == "arn:aws:iam::123456789012:root"
    assert statement["Condition"]["StringEquals"]["sts:ExternalId"] == "external-456"


def test_permission_policy_prefix_scopes_to_folder_children():
    s3_connection = S3Connection(
        id=str(uuid4()),
        name="Seller bucket",
        bucket="seller-bucket",
        region="us-east-1",
        prefix="reports",
        external_id="external-789",
    )

    policy = s3_connections._permission_policy(s3_connection)
    list_statement = policy["Statement"][0]
    get_statement = policy["Statement"][1]

    assert list_statement["Condition"]["StringLike"]["s3:prefix"] == ["reports/*"]
    assert get_statement["Resource"] == "arn:aws:s3:::seller-bucket/reports/*"
    assert not fnmatchcase("reports-archive/file.csv", list_statement["Condition"]["StringLike"]["s3:prefix"][0])
    assert not fnmatchcase("arn:aws:s3:::seller-bucket/reports-archive/file.csv", get_statement["Resource"])


def test_post_stamps_owner_id(client, s3_engine):
    data = _create_connection(client)

    with Session(s3_engine) as session:
        stored = session.get(S3Connection, data["id"])
        assert stored.owner_id == "user-a"


def test_get_lists_only_caller_owned_rows(client, s3_engine):
    created = _create_connection(client)
    foreign = _configured_row(s3_engine, owner_id="user-b")

    response = client.get("/api/s3-connections/")

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [created["id"]]
    assert foreign.id not in [row["id"] for row in response.json()]
    assert "trust_policy" not in response.json()[0] or response.json()[0]["trust_policy"] is None


def test_unauthenticated_request_returns_401(client, monkeypatch):
    app.dependency_overrides.pop(get_current_user, None)
    monkeypatch.setenv("VECTORAIZ_AUTH_ENABLED", "true")

    response = client.get("/api/s3-connections/")

    assert response.status_code == 401


def test_get_missing_returns_404(client):
    response = client.get(f"/api/s3-connections/{uuid4()}")

    assert response.status_code == 404


def test_put_role_arn_rejects_malformed_and_accepts_valid(client):
    created = _create_connection(client)

    bad = client.put(f"/api/s3-connections/{created['id']}/role-arn", json={"role_arn": "bad"})
    assert bad.status_code == 400

    good = client.put(
        f"/api/s3-connections/{created['id']}/role-arn",
        json={"role_arn": "arn:aws:iam::210987654321:role/aim-data"},
    )
    assert good.status_code == 200
    assert good.json()["role_arn"] == "arn:aws:iam::210987654321:role/aim-data"
    assert good.json()["status"] == "configured"


def test_verify_success_sets_verified_and_last_scanned_at(client, s3_engine, monkeypatch):
    connection = _configured_row(s3_engine)
    captured = {}

    class FakeSerialClient:
        async def verify_s3_connection(self, serial, install_token, **kwargs):
            captured["serial"] = serial
            captured["install_token"] = install_token
            captured["kwargs"] = kwargs
            return {
                "success": True,
                "status": "verified",
                "error_message": None,
                "verified_at": "2026-06-01T12:00:00+00:00",
            }

    monkeypatch.setattr(s3_connections, "get_serial_store", lambda: _serial_store())
    monkeypatch.setattr(s3_connections, "SerialClient", FakeSerialClient)

    response = client.post(f"/api/s3-connections/{connection.id}/verify")

    assert response.status_code == 200
    assert response.json()["status"] == "verified"
    assert response.json()["verified_at"] == "2026-06-01T12:00:00+00:00"
    assert captured == {
        "serial": "VZ-test",
        "install_token": "vzit-test",
        "kwargs": {
            "role_arn": connection.role_arn,
            "external_id": connection.external_id,
            "bucket": connection.bucket,
            "region": connection.region,
            "prefix": connection.prefix,
        },
    }
    with Session(s3_engine) as session:
        stored = session.get(S3Connection, connection.id)
        assert stored.status == "verified"
        assert stored.last_scanned_at is not None


def test_verify_broker_error_sets_error(client, s3_engine, monkeypatch):
    connection = _configured_row(s3_engine)

    class FakeSerialClient:
        async def verify_s3_connection(self, *_args, **_kwargs):
            return {
                "success": True,
                "status": "error",
                "error_message": "Access denied by broker",
                "verified_at": None,
            }

    monkeypatch.setattr(s3_connections, "get_serial_store", lambda: _serial_store())
    monkeypatch.setattr(s3_connections, "SerialClient", FakeSerialClient)

    response = client.post(f"/api/s3-connections/{connection.id}/verify")

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["error_message"] == "Access denied by broker"
    with Session(s3_engine) as session:
        stored = session.get(S3Connection, connection.id)
        assert stored.status == "error"
        assert stored.error_message == "Access denied by broker"


def test_verify_not_connected_sets_error_without_crashing(client, s3_engine, monkeypatch):
    connection = _configured_row(s3_engine)
    client_called = False

    class FakeSerialClient:
        async def verify_s3_connection(self, *_args, **_kwargs):
            nonlocal client_called
            client_called = True
            return {"success": True, "status": "verified", "verified_at": "2026-06-01T12:00:00+00:00"}

    monkeypatch.setattr(s3_connections, "get_serial_store", lambda: _serial_store(active=False))
    monkeypatch.setattr(s3_connections, "SerialClient", FakeSerialClient)

    response = client.post(f"/api/s3-connections/{connection.id}/verify")

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["error_message"] == "Connect this install to ai.market to verify S3 access."
    assert client_called is False
    with Session(s3_engine) as session:
        stored = session.get(S3Connection, connection.id)
        assert stored.status == "error"
        assert stored.error_message == "Connect this install to ai.market to verify S3 access."


def test_register_new_s3_object_starts_upload_pipeline(client, s3_engine, monkeypatch, tmp_path):
    connection = _configured_row(s3_engine)
    scan_job = S3ScanJob(id=str(uuid4()), connection_id=connection.id, status="completed")
    obj = S3ObjectMetadata(
        id=str(uuid4()),
        connection_id=connection.id,
        scan_job_id=scan_job.id,
        object_key="exports/report.csv",
        size_bytes=12,
        content_type="text/csv",
        last_modified=datetime.now(timezone.utc),
        etag="etag",
    )
    with Session(s3_engine) as session:
        session.add(scan_job)
        session.add(obj)
        session.commit()
        session.refresh(obj)
        object_id = obj.id

    class FakePresignClient:
        async def presign_object(self, serial, install_token, **kwargs):
            assert serial == "VZ-test"
            assert install_token == "vzit-test"
            assert kwargs == {
                "role_arn": connection.role_arn,
                "bucket": connection.bucket,
                "region": connection.region,
                "object_key": "exports/report.csv",
            }
            return {"success": True, "url": "https://example.test/report.csv", "expires_at": "2026-06-01T12:15:00Z"}

    submitted = []

    class FakeQueue:
        async def submit(self, dataset_id):
            submitted.append(dataset_id)
            return 1

    async def fake_download(url, destination):
        assert url == "https://example.test/report.csv"
        assert destination.parent == tmp_path
        return 12, b"name,age\n"

    monkeypatch.setattr(s3_object_profiler.settings, "upload_directory", str(tmp_path))
    monkeypatch.setattr(s3_object_profiler, "SerialClient", FakePresignClient)
    monkeypatch.setattr(s3_object_profiler, "_download_presigned_url", fake_download)
    monkeypatch.setattr(s3_object_profiler, "get_processing_queue", lambda: FakeQueue())

    response = client.post(f"/api/s3-connections/{connection.id}/objects/{object_id}/register", json={})

    assert response.status_code == 200
    dataset = response.json()["dataset"]
    assert dataset["status"] == "uploaded"
    assert dataset["status"] not in ("s3_linked", "failed")
    assert submitted == [dataset["id"]]

    with Session(s3_engine) as session:
        stored = session.get(DatasetRecord, dataset["id"])
        metadata = session.get(S3ObjectMetadata, object_id)

    assert stored.status == "uploaded"
    assert stored.storage_filename == dataset["storage_filename"]
    assert stored.storage_filename.endswith("_report.csv")
    assert json.loads(stored.metadata_json)["s3_origin"] == {
        "connection_id": connection.id,
        "bucket": connection.bucket,
        "region": connection.region,
        "object_key": "exports/report.csv",
    }
    assert metadata.dataset_id == dataset["id"]

    second = client.post(f"/api/s3-connections/{connection.id}/objects/{object_id}/register", json={})
    assert second.status_code == 200
    assert second.json()["dataset"]["id"] == dataset["id"]
    assert submitted == [dataset["id"]]


def test_user_cannot_get_scan_or_list_objects_on_foreign_connection(client, s3_engine, monkeypatch):
    connection = _configured_row(s3_engine, owner_id="user-b")
    scan_job = S3ScanJob(id=str(uuid4()), connection_id=connection.id, status="completed")
    with Session(s3_engine) as session:
        session.add(scan_job)
        session.add(
            S3ObjectMetadata(
                id=str(uuid4()),
                connection_id=connection.id,
                scan_job_id=scan_job.id,
                object_key="exports/foreign.csv",
                size_bytes=1,
                content_type="text/csv",
                last_modified=datetime.now(timezone.utc),
                etag="etag",
            )
        )
        session.commit()

    scan_called = False

    def _scan_connection(_self, _connection_id):
        nonlocal scan_called
        scan_called = True
        raise AssertionError("foreign connection should be rejected before scan")

    monkeypatch.setattr(s3_connections.S3ScanService, "scan_connection", _scan_connection)

    assert client.get(f"/api/s3-connections/{connection.id}").status_code == 403
    assert client.post(f"/api/s3-connections/{connection.id}/scan").status_code == 403
    assert client.get(f"/api/s3-connections/{connection.id}/objects").status_code == 403
    assert scan_called is False


def test_delete_removes_row(client):
    created = _create_connection(client)

    deleted = client.delete(f"/api/s3-connections/{created['id']}")
    assert deleted.status_code == 204

    missing = client.get(f"/api/s3-connections/{created['id']}")
    assert missing.status_code == 404
