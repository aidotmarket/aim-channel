"""
S3 object profiling for no-copy ingest.

Downloads the customer's own S3 object on-prem via an ai.market presigned URL,
then submits the resulting local file to the existing upload processing queue.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiofiles
import httpx

from app.config import settings
from app.core.database import get_session_context
from app.models.dataset import DatasetRecord, DatasetStatus
from app.services.batch_service import _check_magic_bytes
from app.services.processing_queue import get_processing_queue
from app.services.serial_client import SerialClient

logger = logging.getLogger(__name__)


def _set_dataset_error(dataset_id: str, message: str) -> None:
    with get_session_context() as session:
        dataset = session.get(DatasetRecord, dataset_id)
        if dataset is None:
            return
        try:
            metadata = json.loads(dataset.metadata_json) if dataset.metadata_json else {}
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        metadata["error"] = message
        dataset.status = DatasetStatus.ERROR.value
        dataset.metadata_json = json.dumps(metadata, default=str)
        dataset.updated_at = datetime.now(timezone.utc)
        session.add(dataset)
        session.commit()


async def _download_presigned_url(url: str, destination: Path) -> tuple[int, bytes]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    bytes_written = 0
    magic_header = b""
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async with aiofiles.open(destination, "wb") as out_file:
                async for chunk in response.aiter_bytes(settings.chunk_size):
                    if not chunk:
                        continue
                    bytes_written += len(chunk)
                    if len(magic_header) < 8:
                        magic_header += chunk[: 8 - len(magic_header)]
                    await out_file.write(chunk)
    return bytes_written, magic_header


async def profile_registered_s3_object(
    *,
    dataset_id: str,
    serial: str,
    install_token: str,
    role_arn: str,
    bucket: str,
    region: str,
    object_key: str,
    storage_filename: str,
    file_type: str,
    serial_client: SerialClient | None = None,
) -> None:
    client = serial_client or SerialClient()
    presign = await client.presign_object(
        serial,
        install_token,
        role_arn=role_arn,
        bucket=bucket,
        region=region,
        object_key=object_key,
    )
    if not presign.get("success") or not presign.get("url"):
        _set_dataset_error(dataset_id, "Could not get a read URL from ai.market")
        return

    upload_path = Path(settings.upload_directory) / storage_filename
    try:
        bytes_written, magic_header = await _download_presigned_url(presign["url"], upload_path)
    except Exception:
        logger.exception("Failed downloading S3 object %s for dataset %s", object_key, dataset_id)
        _set_dataset_error(dataset_id, "Could not download the S3 object for profiling")
        return

    extension = f".{file_type.lower().lstrip('.')}"
    if not _check_magic_bytes(magic_header, extension):
        try:
            upload_path.unlink(missing_ok=True)
        except OSError:
            pass
        _set_dataset_error(dataset_id, f"File content does not match extension {extension}")
        return

    with get_session_context() as session:
        dataset = session.get(DatasetRecord, dataset_id)
        if dataset is None:
            return
        dataset.file_size_bytes = bytes_written
        dataset.updated_at = datetime.now(timezone.utc)
        session.add(dataset)
        session.commit()

    await get_processing_queue().submit(dataset_id)
