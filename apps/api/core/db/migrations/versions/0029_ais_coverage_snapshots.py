"""AIS community coverage snapshots.

Revision ID: 0029_ais_coverage_snapshots
Revises: 0028_satellite_assoc_status
Create Date: 2026-09-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_ais_coverage_snapshots"
down_revision = "0028_satellite_assoc_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "ais_coverage_snapshots" in set(sa.inspect(bind).get_table_names()):
        return
    op.create_table(
        "ais_coverage_snapshots",
        sa.Column("snapshot_id", sa.String(length=128), primary_key=True),
        sa.Column("network", sa.String(length=32), nullable=False),
        sa.Column("station_id", sa.String(length=64), nullable=False),
        sa.Column("station_label", sa.String(length=128), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("station_status", sa.String(length=24), nullable=False),
        sa.Column("station_last_received_at", sa.DateTime(), nullable=True),
        sa.Column("online_24h", sa.Float(), nullable=False, server_default="0"),
        sa.Column("max_reception_km", sa.Float(), nullable=True),
        sa.Column("messages_1h", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("vessels_1h", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_ais_coverage_snapshots_network", "ais_coverage_snapshots", ["network"]
    )
    op.create_index(
        "ix_ais_coverage_snapshots_station_id", "ais_coverage_snapshots", ["station_id"]
    )
    op.create_index(
        "ix_ais_coverage_snapshots_observed_at", "ais_coverage_snapshots", ["observed_at"]
    )
    op.create_index(
        "ix_ais_coverage_station_observed",
        "ais_coverage_snapshots",
        ["station_id", "observed_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "ais_coverage_snapshots" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("ais_coverage_snapshots")
