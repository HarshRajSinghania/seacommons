"""satellite observation association_status

Revision ID: 0028_satellite_association_status
Revises: 0027_live_retention_contract
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_satellite_assoc_status"
down_revision = "0027_live_retention_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("satellite_observations")}
    if "association_status" not in cols:
        op.add_column(
            "satellite_observations", sa.Column("association_status", sa.String(length=32), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("satellite_observations")}
    if "association_status" in cols:
        op.drop_column("satellite_observations", "association_status")
