"""Deterministic eolymp-v1 tabular artifact connector."""

from __future__ import annotations

import hashlib
import hmac
import io
import math
import zipfile
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.json as pa_json
import pyarrow.parquet as pq

from app.services.marketplace_action_signer import canonical_json_bytes


SUPPORTED_SUFFIXES = frozenset({".csv", ".tsv", ".json", ".jsonl", ".parquet"})
SUPPRESSED = "suppressed_low_occupancy"


class UnsupportedConnectorShape(ValueError):
    pass


class IncompleteCoverageError(ValueError):
    pass


class BudgetIncompatibleError(ValueError):
    pass


def _suffix(name: str) -> str:
    lowered = name.lower()
    for candidate in sorted(SUPPORTED_SUFFIXES, key=len, reverse=True):
        if lowered.endswith(candidate):
            return candidate
    return ""


def _read_table(name: str, payload: bytes) -> pa.Table:
    suffix = _suffix(name)
    try:
        source = pa.BufferReader(payload)
        if suffix == ".csv":
            return pa_csv.read_csv(source)
        if suffix == ".tsv":
            return pa_csv.read_csv(source, parse_options=pa_csv.ParseOptions(delimiter="\t"))
        if suffix in {".json", ".jsonl"}:
            return pa_json.read_json(source)
        if suffix == ".parquet":
            return pq.read_table(source)
    except Exception as exc:
        raise UnsupportedConnectorShape("supported tabular object could not be decoded") from exc
    raise UnsupportedConnectorShape("artifact contains an unsupported object type")


def _objects(artifact_name: str, payload: bytes) -> list[tuple[str, bytes]]:
    stream = io.BytesIO(payload)
    if zipfile.is_zipfile(stream):
        try:
            with zipfile.ZipFile(stream) as archive:
                entries = [entry for entry in archive.infolist() if not entry.is_dir()]
                if not entries or any(_suffix(entry.filename) not in SUPPORTED_SUFFIXES for entry in entries):
                    raise IncompleteCoverageError("artifact traversal is not fully supported")
                names = [entry.filename for entry in entries]
                if len(set(names)) != len(names):
                    raise IncompleteCoverageError("artifact traversal identities are ambiguous")
                return [(name, archive.read(name)) for name in sorted(names)]
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise UnsupportedConnectorShape("archive could not be decoded") from exc
    if _suffix(artifact_name) not in SUPPORTED_SUFFIXES:
        raise UnsupportedConnectorShape("artifact type is unsupported")
    return [("registered-root", payload)]


def _type_name(data_type: pa.DataType) -> str:
    if pa.types.is_boolean(data_type):
        return "boolean"
    if pa.types.is_integer(data_type):
        return "integer"
    if pa.types.is_floating(data_type) or pa.types.is_decimal(data_type):
        return "number"
    if pa.types.is_timestamp(data_type) or pa.types.is_date(data_type) or pa.types.is_time(data_type):
        return "timestamp"
    if pa.types.is_binary(data_type) or pa.types.is_large_binary(data_type):
        return "binary"
    if pa.types.is_null(data_type):
        return "null"
    return "string"


def _canonical_local_value(value: Any) -> bytes:
    if isinstance(value, bytes):
        return b"bytes:" + value
    if isinstance(value, (datetime, date, time)):
        return ("time:" + value.isoformat()).encode("utf-8")
    if isinstance(value, Decimal):
        return ("decimal:" + format(value, "f")).encode("utf-8")
    return canonical_json_bytes(value)


def _hll_estimate(values: Iterable[Any], seed: bytes) -> int:
    registers = [0] * 128
    for value in values:
        digest = hashlib.sha256(seed + b"\x00" + _canonical_local_value(value)).digest()
        number = int.from_bytes(digest[:8], "big")
        index = number & 127
        remainder = number >> 7
        rank = 58 if remainder == 0 else 58 - remainder.bit_length()
        registers[index] = max(registers[index], rank)
    denominator = sum(2.0 ** -register for register in registers)
    estimate = 0.7213 / (1 + 1.079 / 128) * 128 * 128 / denominator
    empty = registers.count(0)
    if empty:
        estimate = 128 * math.log(128 / empty)
    return int(round(estimate))


def _histogram(values: list[Any], bounds: tuple[int, ...]) -> list[int]:
    counts = [0] * (len(bounds) + 1)
    for value in values:
        length = len(value if isinstance(value, bytes) else str(value).encode("utf-8"))
        index = next((i for i, bound in enumerate(bounds) if length <= bound), len(bounds))
        counts[index] += 1
    return counts


def _numeric_buckets(values: list[Any], boundaries: tuple[float, ...]) -> list[int]:
    counts = [0] * (len(boundaries) + 1)
    for value in values:
        number = float(value)
        index = next((i for i, boundary in enumerate(boundaries) if number < boundary), len(boundaries))
        counts[index] += 1
    return counts


class EolympConnectorV1:
    connector_type = "eolymp"
    connector_version = "eolymp-v1"

    def scan_bytes(
        self,
        *,
        artifact_name: str,
        payload: bytes,
        commitment_key: bytes,
        source_binding: bytes,
        deterministic_seed: str,
        minimum_aggregate_occupancy: int,
        length_bounds: tuple[int, ...],
        numeric_boundaries: tuple[float, ...],
        max_inference_input_tokens: int,
        preview_requested: bool,
    ) -> dict[str, Any]:
        source_objects = _objects(artifact_name, payload)
        facts: list[dict[str, Any]] = []
        preview: list[dict[str, Any]] = []
        seed = bytes.fromhex(deterministic_seed)

        for identity, object_payload in source_objects:
            table = _read_table(identity if identity != "registered-root" else artifact_name, object_payload)
            object_id = hmac.new(
                commitment_key,
                b"object\x00" + source_binding + b"\x00" + identity.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            column_names = list(table.column_names)
            columns = [table.column(index).combine_chunks() for index in range(table.num_columns)]
            column_types = [_type_name(column.type) for column in columns]
            null_rates: list[Any] = []
            distinct_counts: list[Any] = []
            length_histograms: list[Any] = []
            numeric_buckets: list[Any] = []

            for index, column in enumerate(columns):
                values = [value for value in column.to_pylist() if value is not None]
                local_exact_distinct = len({_canonical_local_value(value) for value in values})
                low_occupancy = (
                    table.num_rows < minimum_aggregate_occupancy
                    or local_exact_distinct < minimum_aggregate_occupancy
                )
                if low_occupancy:
                    null_rates.append(SUPPRESSED)
                    distinct_counts.append(SUPPRESSED)
                    length_histograms.append(SUPPRESSED)
                    numeric_buckets.append(SUPPRESSED)
                    continue
                null_rates.append(f"{column.null_count / table.num_rows:.6f}")
                distinct_counts.append(
                    {
                        "estimate": _hll_estimate(values, seed + index.to_bytes(4, "big")),
                        "algorithm": "hll-sha256-v1",
                        "relative_error_ppm": 91924,
                    }
                )
                length_histograms.append(
                    _histogram(values, length_bounds) if column_types[index] in {"string", "binary"} else None
                )
                numeric_buckets.append(
                    _numeric_buckets(values, numeric_boundaries)
                    if column_types[index] in {"integer", "number"}
                    else None
                )

            fact = {
                "object_id": object_id,
                "column_names": column_names,
                "column_types": column_types,
                "null_rate": null_rates,
                "approx_distinct_count": distinct_counts,
                "length_histograms": length_histograms,
                "numeric_range_buckets": numeric_buckets,
                "row_count": table.num_rows,
                "row_count_method": "exact",
            }
            facts.append(fact)
            if preview_requested:
                preview.append(
                    {
                        "object_id": object_id,
                        "column_names": column_names,
                        "column_types": column_types,
                        "row_count": table.num_rows,
                        "row_count_method": "exact",
                    }
                )

        facts.sort(key=lambda item: item["object_id"])
        preview.sort(key=lambda item: item["object_id"])
        result = {
            "coverage": {
                "objects_discovered": len(source_objects),
                "objects_scanned": len(facts),
                "objects_skipped_by_reason": {},
                "skipped": [],
            },
            "objects": facts,
            "schema_preview": preview if preview_requested else None,
        }
        if len(canonical_json_bytes(result)) > max_inference_input_tokens * 4:
            raise BudgetIncompatibleError("complete artifact exceeds the signed inference budget")
        return result
