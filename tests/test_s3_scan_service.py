from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, select

from app.main import app
from app.models.dataset import DatasetRecord
from app.models.s3_connection import S3Connection
from app.models.s3_object_metadata import S3ObjectMetadata
from app.models.s3_scan_job import S3ScanJob
from app.routers import s3_connections
from app.services import fulfillment_service, s3_scan_service
from app.services.fulfillment_service import FulfillmentService
from app.services.s3_scan_service import S3ScanService


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
def session_context(s3_engine, monkeypatch):
    @contextmanager
    def _session_context():
        with Session(s3_engine) as session:
            yield session

    monkeypatch.setattr(s3_scan_service, "get_session_context", _session_context)
    monkeypatch.setattr(s3_connections, "get_session_context", _session_context)
    monkeypatch.setattr(fulfillment_service, "get_session_context", _session_context)
    return _session_context


@pytest.fixture
def client(session_context):
    return TestClient(app)


class FakeSerialClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def list_s3_objects(self, serial, install_token, **kwargs):
        self.calls.append(
            {
                "serial": serial,
                "install_token": install_token,
                **kwargs,
            }
        )
        if kwargs.get("continuation_token"):
            return self.pages[1]
        if self.calls and len(self.calls) > 1:
            return self.pages[len(self.calls) - 1]
        return self.pages[0]


class ExplodingObjectClient:
    def get_object(self, **_kwargs):
        raise AssertionError("scan must not fetch object bodies")

    def list_objects_v2(self, **_kwargs):
        raise AssertionError("scan must not use local boto3 listing")


class FakeSerialClientWithObjectBodyMethods(FakeSerialClient):
    def __init__(self, pages):
        super().__init__(pages)
        self.s3 = ExplodingObjectClient()

    async def get_object(self, **_kwargs):
        raise AssertionError("scan must not fetch object bodies")

    async def download_fileobj(self, *_args, **_kwargs):
        raise AssertionError("scan must not fetch object bodies")


def _connection(**overrides) -> S3Connection:
    values = {
        "id": str(uuid4()),
        "owner_id": "mock_user_auth_disabled",
        "name": "Seller bucket",
        "bucket": "seller-bucket",
        "region": "us-east-1",
        "prefix": "exports/",
        "role_arn": "arn:aws:iam::210987654321:role/aim-data",
        "external_id": str(uuid4()),
        "status": "verified",
    }
    values.update(overrides)
    return S3Connection(**values)


def _add_connection(session_context, **overrides) -> S3Connection:
    connection = _connection(**overrides)
    with session_context() as session:
        session.add(connection)
        session.commit()
        session.refresh(connection)
        session.expunge(connection)
    return connection


def _page(*objects, truncated=False, token=None):
    response = {
        "success": True,
        "status": "listed",
        "is_truncated": truncated,
        "objects": list(objects),
        "next_continuation_token": token,
        "error_message": None,
    }
    return response


def _object(key: str, size: int = 123):
    return {
        "key": key,
        "size": size,
        "etag": '"etag"',
        "last_modified": "2026-05-29T00:00:00Z",
        "storage_class": "STANDARD",
    }


@pytest.mark.asyncio
async def test_scan_persists_one_row_per_object(session_context):
    connection = _add_connection(session_context)
    serial_client = FakeSerialClient(
        [
            _page(_object("exports/a.csv"), truncated=True, token="next"),
            _page(_object("exports/b.json", 456)),
        ]
    )

    scan_job = await S3ScanService(serial_client).scan_connection(connection.id, "VZ-test", "vzit-test")

    assert scan_job.status == "completed"
    assert scan_job.objects_enumerated == 2
    assert serial_client.calls[0] == {
        "serial": "VZ-test",
        "install_token": "vzit-test",
        "role_arn": "arn:aws:iam::210987654321:role/aim-data",
        "bucket": "seller-bucket",
        "region": "us-east-1",
        "prefix": "exports/",
        "continuation_token": None,
    }
    assert serial_client.calls[1]["continuation_token"] == "next"
    with session_context() as session:
        objects = session.exec(select(S3ObjectMetadata).order_by(S3ObjectMetadata.object_key)).all()
        stored_connection = session.get(S3Connection, connection.id)
    assert [obj.object_key for obj in objects] == ["exports/a.csv", "exports/b.json"]
    assert objects[0].content_type == "text/csv"
    assert objects[1].size_bytes == 456
    assert stored_connection.last_scanned_at is not None


@pytest.mark.asyncio
async def test_rescan_is_idempotent_and_preserves_dataset_id(session_context):
    connection = _add_connection(session_context)
    dataset = DatasetRecord(
        id=str(uuid4()),
        original_filename="a.csv",
        storage_filename="exports/a.csv",
        file_type="csv",
        file_size_bytes=123,
        status="s3_linked",
    )
    dataset_id = dataset.id
    scan_job = S3ScanJob(id=str(uuid4()), connection_id=connection.id, status="completed")
    original_object_id = str(uuid4())
    with session_context() as session:
        session.add(dataset)
        session.add(scan_job)
        session.add(
            S3ObjectMetadata(
                id=original_object_id,
                connection_id=connection.id,
                scan_job_id=scan_job.id,
                object_key="exports/a.csv",
                size_bytes=1,
                content_type="text/csv",
                last_modified=datetime.now(timezone.utc),
                etag="old",
                dataset_id=dataset.id,
            )
        )
        session.commit()

    serial_client = FakeSerialClient([_page(_object("exports/a.csv", 999))])

    await S3ScanService(serial_client).scan_connection(connection.id, "VZ-test", "vzit-test")

    with session_context() as session:
        objects = session.exec(select(S3ObjectMetadata)).all()
    assert len(objects) == 1
    assert objects[0].id == original_object_id
    assert objects[0].dataset_id == dataset_id
    assert objects[0].size_bytes == 999


@pytest.mark.asyncio
async def test_scan_broker_error_marks_failed_without_raw_detail(session_context):
    connection = _add_connection(session_context)
    raw = "An error occurred (AccessDenied) when calling the AssumeRole operation"
    serial_client = FakeSerialClient(
        [
            {
                "success": True,
                "status": "error",
                "objects": [],
                "next_continuation_token": None,
                "is_truncated": False,
                "error_message": "AccessDenied",
            }
        ]
    )

    scan_job = await S3ScanService(serial_client).scan_connection(connection.id, "VZ-test", "vzit-test")

    assert scan_job.status == "failed"
    assert "AccessDenied" in scan_job.error_message
    assert raw not in scan_job.error_message
    with session_context() as session:
        stored = session.get(S3ScanJob, scan_job.id)
    assert stored.status == "failed"


@pytest.mark.asyncio
async def test_scan_maps_metadata_and_never_fetches_object_bodies(session_context):
    connection = _add_connection(session_context)
    serial_client = FakeSerialClientWithObjectBodyMethods(
        [
            _page(
                {
                    "key": "exports/report.parquet",
                    "size": 2048,
                    "last_modified": "2026-05-29T12:34:56+00:00",
                    "storage_class": "STANDARD",
                    "etag": '"metadata-etag"',
                }
            )
        ]
    )

    scan_job = await S3ScanService(serial_client).scan_connection(connection.id, "VZ-test", "vzit-test")

    assert scan_job.status == "completed"
    with session_context() as session:
        metadata = session.exec(select(S3ObjectMetadata)).one()
    assert metadata.object_key == "exports/report.parquet"
    assert metadata.size_bytes == 2048
    assert metadata.last_modified.replace(tzinfo=timezone.utc) == datetime(2026, 5, 29, 12, 34, 56, tzinfo=timezone.utc)
    assert metadata.etag == '"metadata-etag"'
    assert metadata.content_type == "application/octet-stream"


def test_register_endpoint_links_object_and_creates_dataset(client, session_context):
    connection = _add_connection(session_context)
    scan_job = S3ScanJob(id=str(uuid4()), connection_id=connection.id, status="completed")
    metadata = S3ObjectMetadata(
        id=str(uuid4()),
        connection_id=connection.id,
        scan_job_id=scan_job.id,
        object_key="exports/report.csv",
        size_bytes=789,
        content_type="text/csv",
        last_modified=datetime.now(timezone.utc),
        etag='"etag"',
    )
    metadata_id = metadata.id
    with session_context() as session:
        session.add(scan_job)
        session.add(metadata)
        session.commit()

    response = client.post(
        f"/api/s3-connections/{connection.id}/objects/{metadata_id}/register",
        json={"listing_id": "lst_123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dataset"]["original_filename"] == "report.csv"
    assert body["dataset"]["file_type"] == "csv"
    assert body["dataset"]["file_size_bytes"] == 789
    assert body["dataset"]["status"] == "s3_linked"
    assert body["dataset"]["storage_filename"] == "exports/report.csv"
    assert body["dataset"]["listing_id"] == "lst_123"
    assert body["object"]["dataset_id"] == body["dataset"]["id"]

    second = client.post(
        f"/api/s3-connections/{connection.id}/objects/{metadata_id}/register",
        json={"listing_id": "lst_123"},
    )
    assert second.status_code == 200
    assert second.json()["dataset"]["id"] == body["dataset"]["id"]


def test_objects_endpoint_paginates_and_filters(client, session_context):
    connection = _add_connection(session_context)
    dataset = DatasetRecord(
        id=str(uuid4()),
        original_filename="linked.csv",
        storage_filename="exports/linked.csv",
        file_type="csv",
        file_size_bytes=1,
        status="s3_linked",
    )
    scan_job = S3ScanJob(id=str(uuid4()), connection_id=connection.id, status="completed")
    with session_context() as session:
        session.add(dataset)
        session.add(scan_job)
        session.add(
            S3ObjectMetadata(
                id=str(uuid4()),
                connection_id=connection.id,
                scan_job_id=scan_job.id,
                object_key="exports/linked.csv",
                size_bytes=1,
                content_type="text/csv",
                last_modified=datetime.now(timezone.utc),
                etag="etag",
                dataset_id=dataset.id,
            )
        )
        session.add(
            S3ObjectMetadata(
                id=str(uuid4()),
                connection_id=connection.id,
                scan_job_id=scan_job.id,
                object_key="exports/unlinked.csv",
                size_bytes=1,
                content_type="text/csv",
                last_modified=datetime.now(timezone.utc),
                etag="etag",
            )
        )
        session.commit()

    response = client.get(f"/api/s3-connections/{connection.id}/objects", params={"dataset_linked": False})

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["object_key"] == "exports/unlinked.csv"


def test_fulfillment_resolves_registered_s3_dataset(session_context):
    connection = _add_connection(session_context)
    dataset = DatasetRecord(
        id=str(uuid4()),
        original_filename="listing.csv",
        storage_filename="exports/listing.csv",
        file_type="csv",
        file_size_bytes=321,
        status="s3_linked",
        listing_id="listing-123",
    )
    dataset_id = dataset.id
    scan_job = S3ScanJob(id=str(uuid4()), connection_id=connection.id, status="completed")
    metadata = S3ObjectMetadata(
        id=str(uuid4()),
        connection_id=connection.id,
        scan_job_id=scan_job.id,
        object_key="exports/listing.csv",
        size_bytes=321,
        content_type="text/csv",
        last_modified=datetime.now(timezone.utc),
        etag="etag",
        dataset_id=dataset.id,
    )
    metadata_id = metadata.id
    with session_context() as session:
        session.add(dataset)
        session.add(scan_job)
        session.add(metadata)
        session.commit()

    service = FulfillmentService(SimpleNamespace())
    found_dataset, file_path = service._find_dataset("listing-123")
    s3_object = service._find_s3_object(found_dataset)

    assert file_path is None
    assert found_dataset.id == dataset_id
    assert s3_object is not None
    found_connection, found_metadata = s3_object
    assert found_connection.id == connection.id
    assert found_metadata.id == metadata_id


def test_register_rejects_unowned_existing_dataset(client, session_context):
    """A connection cannot attach its object to a dataset it does not own (S729 sec review)."""
    connection = _add_connection(session_context)
    foreign = DatasetRecord(
        id=str(uuid4()),
        original_filename="foreign.csv",
        storage_filename="foreign.csv",
        file_type="csv",
        file_size_bytes=1,
        status="s3_linked",
        listing_id="foreign-listing",
    )
    scan_job = S3ScanJob(id=str(uuid4()), connection_id=connection.id, status="completed")
    metadata = S3ObjectMetadata(
        id=str(uuid4()),
        connection_id=connection.id,
        scan_job_id=scan_job.id,
        object_key="exports/mine.csv",
        size_bytes=1,
        content_type="text/csv",
        last_modified=datetime.now(timezone.utc),
        etag="etag",
    )
    metadata_id = metadata.id
    with session_context() as session:
        session.add(foreign)
        session.add(scan_job)
        session.add(metadata)
        session.commit()

    resp = client.post(
        f"/api/s3-connections/{connection.id}/objects/{metadata_id}/register",
        json={"listing_id": "foreign-listing"},
    )
    assert resp.status_code == 403
