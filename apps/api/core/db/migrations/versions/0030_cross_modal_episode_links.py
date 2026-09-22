"""Canonical episode links for Radio/Satellite evidence.

Revision ID: 0030_cross_modal_episode_links
Revises: 0029_ais_coverage_snapshots
Create Date: 2026-09-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_cross_modal_episode_links"
down_revision = "0029_ais_coverage_snapshots"
branch_labels = None
depends_on = None


def _add_episode_link(table: str, index_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns(table)}
    if "episode_id" not in cols:
        op.add_column(
            table,
            sa.Column("episode_id", sa.String(length=128), nullable=True),
        )
    indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes(table)}
    if index_name not in indexes:
        op.create_index(index_name, table, ["episode_id"])


def upgrade() -> None:
    _add_episode_link(
        "satellite_observations",
        "ix_satellite_observations_episode_id",
    )
    _add_episode_link(
        "radio_ais_associations",
        "ix_radio_ais_associations_episode_id",
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table, index_name in (
        ("radio_ais_associations", "ix_radio_ais_associations_episode_id"),
        ("satellite_observations", "ix_satellite_observations_episode_id"),
    ):
        inspector = sa.inspect(bind)
        indexes = {ix["name"] for ix in inspector.get_indexes(table)}
        if index_name in indexes:
            op.drop_index(index_name, table_name=table)
        cols = {c["name"] for c in sa.inspect(bind).get_columns(table)}
        if "episode_id" in cols:
            op.drop_column(table, "episode_id")
