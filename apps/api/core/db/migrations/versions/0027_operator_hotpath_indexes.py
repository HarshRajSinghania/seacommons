# SPDX-License-Identifier: AGPL-3.0-or-later
"""indexes for operator-dashboard hot paths

Revision ID: 0027_operator_hotpath_indexes
Revises: 0026_radio_ais_associations
"""
from __future__ import annotations

from alembic import op

revision = "0027_operator_hotpath_indexes"
down_revision = "0026_radio_ais_associations"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_intel_events_received_at", "intel_events", ["received_at"]),
    ("ix_maritime_episodes_updated_at", "maritime_episodes", ["updated_at"]),
    (
        "ix_investigation_hypotheses_updated_at",
        "investigation_hypotheses",
        ["updated_at"],
    ),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns, if_not_exists=True)


def downgrade() -> None:
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)
