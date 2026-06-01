"""
BQ-VZ-SHARED-SEARCH: Portal Service — search execution with column restrictions.

Uses DuckDB keyword matching, then filters results to only include
display_columns configured per-dataset.
"""

import json
import logging
from typing import List, Optional

from app.models.portal import get_portal_config
from app.schemas.portal import (
    DatasetPortalConfig,
    PortalDatasetInfo,
    PortalSearchResult,
)
from app.services.processing_service import get_processing_service
from app.services.duckdb_service import ephemeral_duckdb_service
from app.utils.sanitization import sql_quote_literal

logger = logging.getLogger(__name__)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class PortalService:
    """Portal search with ACL and column restrictions."""

    def get_visible_datasets(self) -> List[PortalDatasetInfo]:
        """Return datasets that are portal_visible=True."""
        config = get_portal_config()
        processing_svc = get_processing_service()
        result = []

        for dataset_id, ds_config in config.datasets.items():
            if not ds_config.portal_visible:
                continue

            record = processing_svc.get_dataset(dataset_id)
            if not record:
                continue

            # Row count is stored in metadata_json
            row_count = 0
            try:
                meta = json.loads(record.metadata_json) if record.metadata_json else {}
                row_count = meta.get("row_count", 0) or 0
            except (json.JSONDecodeError, TypeError):
                pass

            result.append(PortalDatasetInfo(
                dataset_id=dataset_id,
                name=record.original_filename or dataset_id,
                description=None,
                row_count=row_count,
                searchable_columns=ds_config.search_columns,
            ))

        return result

    def is_dataset_visible(self, dataset_id: str) -> bool:
        """Check if a dataset is portal-visible (M1 ACL)."""
        config = get_portal_config()
        ds_config = config.datasets.get(dataset_id)
        return ds_config is not None and ds_config.portal_visible

    def get_dataset_portal_config(self, dataset_id: str) -> Optional[DatasetPortalConfig]:
        """Get portal config for a specific dataset."""
        config = get_portal_config()
        return config.datasets.get(dataset_id)

    def search_dataset(
        self,
        dataset_id: str,
        query: str,
        limit: int = 20,
        offset: int = 0,
    ) -> PortalSearchResult:
        """Search a dataset and return only display_columns."""
        ds_config = self.get_dataset_portal_config(dataset_id)
        if not ds_config or not ds_config.portal_visible:
            raise ValueError(f"Dataset '{dataset_id}' is not available on portal")

        # Cap limit to dataset max_results
        effective_limit = min(limit, ds_config.max_results)

        processing_svc = get_processing_service()
        record = processing_svc.get_dataset(dataset_id)
        if not record or not record.processed_path:
            raise ValueError(f"Dataset '{dataset_id}' is not processed")

        raw_results = self._keyword_search(
            processed_path=str(record.processed_path),
            query=query,
            search_columns=ds_config.search_columns,
            limit=effective_limit,
            offset=offset,
        )

        # Filter to display_columns only
        filtered_results = []
        display_cols = set(ds_config.display_columns) if ds_config.display_columns else None

        for r in raw_results:
            row_data = r.get("row_data", {})
            if display_cols:
                row_data = {k: v for k, v in row_data.items() if k in display_cols}

            filtered_results.append({
                "score": r.get("score", 1.0),
                "row_data": row_data,
                "text_content": r.get("text_content", ""),
            })

        dataset_name = record.original_filename if record else dataset_id

        return PortalSearchResult(
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            total_count=len(filtered_results),
            results=filtered_results,
            query=query,
        )

    def _keyword_search(
        self,
        processed_path: str,
        query: str,
        search_columns: List[str],
        limit: int,
        offset: int,
    ) -> List[dict]:
        """Run simple case-insensitive DuckDB keyword matching over processed data."""
        if not query.strip():
            return []

        with ephemeral_duckdb_service() as duckdb:
            escaped_path = sql_quote_literal(processed_path)
            conn = duckdb.create_ephemeral_connection(memory_limit="256MB", threads=1)
            try:
                conn.execute(
                    f"CREATE OR REPLACE VIEW portal_data AS "
                    f"SELECT * FROM read_parquet('{escaped_path}')"
                )
                table_info = conn.execute("PRAGMA table_info('portal_data')").fetchall()
                columns = [row[1] for row in table_info]
                if not columns:
                    return []

                allowed = set(columns)
                candidate_columns = [c for c in search_columns if c in allowed] or columns
                predicates = [
                    f"lower(CAST({_quote_identifier(col)} AS VARCHAR)) LIKE lower(?)"
                    for col in candidate_columns
                ]
                where_sql = " OR ".join(predicates)
                params = [f"%{query}%"] * len(predicates)
                sql = (
                    "SELECT * FROM portal_data "
                    f"WHERE {where_sql} "
                    f"LIMIT {int(limit)} OFFSET {int(offset)}"
                )
                result = conn.execute(sql, params)
                result_columns = [desc[0] for desc in result.description]
                rows = result.fetchall()
            finally:
                conn.close()

        matches = []
        for row in rows:
            row_data = dict(zip(result_columns, row))
            text_content = " ".join(str(row_data.get(col, "")) for col in candidate_columns)
            matches.append({
                "score": 1.0,
                "row_data": row_data,
                "text_content": text_content[:500],
            })
        return matches

    def search_all_visible(
        self,
        query: str,
        limit: int = 20,
    ) -> List[PortalSearchResult]:
        """Search across all portal-visible datasets."""
        datasets = self.get_visible_datasets()
        results = []
        for ds in datasets:
            try:
                result = self.search_dataset(ds.dataset_id, query, limit)
                if result.results:
                    results.append(result)
            except Exception as e:
                logger.warning("Portal search failed for dataset %s: %s", ds.dataset_id, e)
                continue
        return results


# Singleton
_portal_service: Optional[PortalService] = None


def get_portal_service() -> PortalService:
    global _portal_service
    if _portal_service is None:
        _portal_service = PortalService()
    return _portal_service
