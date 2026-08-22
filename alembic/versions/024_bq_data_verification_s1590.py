"""Add local data-verification run state.

Revision ID: 024_bq_data_verification_s1590
Revises: 023_s3_scan_job_sampled_stats
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "024_bq_data_verification_s1590"
down_revision: Union[str, None] = "023_s3_scan_job_sampled_stats"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "data_verification_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("listing_id", sa.String(length=255), nullable=False),
        sa.Column("source_handle_id", sa.String(length=128), nullable=False),
        sa.Column("verification_id", sa.String(length=128), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("owner_authorization_id", sa.String(length=128), nullable=False),
        sa.Column("accepted_at_utc", sa.DateTime(), nullable=True),
        sa.Column("preview_requested", sa.Boolean(), nullable=False),
        sa.Column("publication_terms_ack", sa.Boolean(), nullable=False),
        sa.Column("corpus_ack", sa.Boolean(), nullable=False),
        sa.Column("d6_json", sa.Text(), nullable=False),
        sa.Column("probe_json", sa.Text(), nullable=False),
        sa.Column("quote_json", sa.Text(), nullable=True),
        sa.Column("payment_status_json", sa.Text(), nullable=True),
        sa.Column("report_ingest_json", sa.Text(), nullable=True),
        sa.Column("report_json", sa.Text(), nullable=True),
        sa.Column("d8_json", sa.Text(), nullable=True),
        sa.Column("start_claimed", sa.Boolean(), nullable=False),
        sa.Column("scan_claimed", sa.Boolean(), nullable=False),
        sa.Column("withdraw_requested_at_utc", sa.DateTime(), nullable=True),
        sa.Column("withdrawn_at_utc", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_data_verification_run_idempotency"),
        sa.UniqueConstraint("verification_id", name="uq_data_verification_run_verification"),
    )
    op.create_index("ix_data_verification_runs_dataset_id", "data_verification_runs", ["dataset_id"])
    op.create_index("ix_data_verification_runs_listing_id", "data_verification_runs", ["listing_id"])
    op.create_index("ix_data_verification_dataset_created", "data_verification_runs", ["dataset_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_data_verification_dataset_created", table_name="data_verification_runs")
    op.drop_index("ix_data_verification_runs_listing_id", table_name="data_verification_runs")
    op.drop_index("ix_data_verification_runs_dataset_id", table_name="data_verification_runs")
    op.drop_table("data_verification_runs")
