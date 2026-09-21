"""durable Live retention contract on intel_events and investigation_hypotheses

Revision ID: 0027_live_retention_contract
Revises: 0027_operator_hotpath_indexes
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_live_retention_contract"
down_revision = "0027_operator_hotpath_indexes"
branch_labels = None
depends_on = None

_INTEL_EVENTS_COLUMNS = [
    ("live_entered_at", sa.DateTime()),
    ("last_qualified_observation_at", sa.DateTime()),
    ("live_expires_at", sa.DateTime()),
    ("live_case_key", sa.String(length=160)),
]

_HYPOTHESIS_COLUMNS = [
    ("live_entered_at", sa.DateTime()),
    ("last_qualified_observation_at", sa.DateTime()),
    ("live_expires_at", sa.DateTime()),
]


def upgrade() -> None:
    bind = op.get_bind()
    intel_cols = {c["name"] for c in sa.inspect(bind).get_columns("intel_events")}
    for name, coltype in _INTEL_EVENTS_COLUMNS:
        if name not in intel_cols:
            op.add_column("intel_events", sa.Column(name, coltype, nullable=True))
    if "ix_intel_events_live_expires_at" not in {
        ix["name"] for ix in sa.inspect(bind).get_indexes("intel_events")
    }:
        op.create_index("ix_intel_events_live_expires_at", "intel_events", ["live_expires_at"])
    if "ix_intel_events_live_case_key" not in {
        ix["name"] for ix in sa.inspect(bind).get_indexes("intel_events")
    }:
        op.create_index("ix_intel_events_live_case_key", "intel_events", ["live_case_key"])

    hyp_cols = {c["name"] for c in sa.inspect(bind).get_columns("investigation_hypotheses")}
    for name, coltype in _HYPOTHESIS_COLUMNS:
        if name not in hyp_cols:
            op.add_column("investigation_hypotheses", sa.Column(name, coltype, nullable=True))
    if "ix_investigation_hypotheses_live_expires_at" not in {
        ix["name"] for ix in sa.inspect(bind).get_indexes("investigation_hypotheses")
    }:
        op.create_index(
            "ix_investigation_hypotheses_live_expires_at",
            "investigation_hypotheses",
            ["live_expires_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    hyp_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("investigation_hypotheses")}
    if "ix_investigation_hypotheses_live_expires_at" in hyp_indexes:
        op.drop_index("ix_investigation_hypotheses_live_expires_at", table_name="investigation_hypotheses")
    hyp_cols = {c["name"] for c in sa.inspect(bind).get_columns("investigation_hypotheses")}
    for name, _ in _HYPOTHESIS_COLUMNS:
        if name in hyp_cols:
            op.drop_column("investigation_hypotheses", name)

    intel_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes("intel_events")}
    if "ix_intel_events_live_case_key" in intel_indexes:
        op.drop_index("ix_intel_events_live_case_key", table_name="intel_events")
    if "ix_intel_events_live_expires_at" in intel_indexes:
        op.drop_index("ix_intel_events_live_expires_at", table_name="intel_events")
    intel_cols = {c["name"] for c in sa.inspect(bind).get_columns("intel_events")}
    for name, _ in _INTEL_EVENTS_COLUMNS:
        if name in intel_cols:
            op.drop_column("intel_events", name)
