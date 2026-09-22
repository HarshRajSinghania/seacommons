"""Index SourceObservation lineage lookup.

Revision ID: 0031_source_obs_lineage_idx
Revises: 0030_cross_modal_episode_links
Create Date: 2026-09-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_source_obs_lineage_idx"
down_revision = "0030_cross_modal_episode_links"
branch_labels = None
depends_on = None

_INDEX = "ix_source_observations_payload_received"


def upgrade() -> None:
    bind = op.get_bind()
    indexes = {
        row["name"]
        for row in sa.inspect(bind).get_indexes("source_observations")
    }
    if _INDEX not in indexes:
        op.create_index(
            _INDEX,
            "source_observations",
            ["raw_payload_hash", "received_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {
        row["name"]
        for row in sa.inspect(bind).get_indexes("source_observations")
    }
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name="source_observations")
