"""Таблиця підтверджених частин published_messages.

Revision ID: 0001
Revises:
Create Date: 2026-11-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "published_messages"


def upgrade() -> None:
    tz = sa.DateTime(timezone=True)
    jsonb = postgresql.JSONB()
    op.create_table(
        TABLE,
        sa.Column("publication_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("part_key", sa.Text(), nullable=False),
        sa.Column("digest_key", sa.Text(), nullable=False),
        sa.Column("part_index", sa.Integer(), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column("series_manifest_hash", sa.Text(), nullable=True),
        sa.Column("target_channel_id", sa.Text(), nullable=False),
        sa.Column("destination_peer_id", sa.BigInteger(), nullable=False),
        sa.Column("destination_message_id", sa.BigInteger(), nullable=False),
        sa.Column("window_start_utc", tz, nullable=False),
        sa.Column("window_end_utc", tz, nullable=False),
        sa.Column("scheduled_at_utc", tz, nullable=False),
        sa.Column("published_at_utc", tz, nullable=False),
        sa.Column("confirmed_at_utc", tz, nullable=False),
        sa.Column("confirmation_method", sa.Text(), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column("entities", jsonb, nullable=True),
        sa.Column("payload_hash", sa.Text(), nullable=True),
        sa.Column("random_id", sa.BigInteger(), nullable=True),
        sa.Column("config_version", sa.Text(), nullable=True),
        sa.Column("schedule_revision", sa.Integer(), nullable=True),
        sa.Column("source_order_snapshot", jsonb, nullable=True),
        sa.Column("items", jsonb, nullable=True),
        sa.Column("source_message_ids", jsonb, nullable=True),
        sa.Column("coverage_summary", jsonb, nullable=True),
        sa.Column("ai_usage_summary", jsonb, nullable=True),
        sa.Column("usage_scope", sa.Text(), nullable=True),
        sa.Column("metadata_recovery", sa.Text(), nullable=False),
        sa.Column("own_edit_history", jsonb, nullable=True),
        sa.PrimaryKeyConstraint("publication_id", name=op.f("pk_published_messages")),
        sa.UniqueConstraint("part_key", name=op.f("uq_published_messages_part_key")),
        sa.UniqueConstraint(
            "digest_key", "part_index", name=op.f("uq_published_messages_digest_key_part_index")
        ),
        sa.UniqueConstraint(
            "destination_peer_id",
            "destination_message_id",
            name="uq_published_messages_destination",
        ),
        sa.CheckConstraint(
            "part_count >= 1", name=op.f("ck_published_messages_part_count_positive")
        ),
        sa.CheckConstraint(
            "part_index BETWEEN 1 AND part_count",
            name=op.f("ck_published_messages_part_index_range"),
        ),
        sa.CheckConstraint(
            "window_start_utc < window_end_utc", name=op.f("ck_published_messages_window_order")
        ),
        sa.CheckConstraint(
            "confirmation_method IN ('ack', 'reconciled')",
            name=op.f("ck_published_messages_confirmation_method"),
        ),
        sa.CheckConstraint(
            "metadata_recovery IN ('complete', 'partial')",
            name=op.f("ck_published_messages_metadata_recovery"),
        ),
        sa.CheckConstraint(
            "usage_scope IS NULL OR usage_scope IN ('series', 'none')",
            name=op.f("ck_published_messages_usage_scope"),
        ),
        sa.CheckConstraint(
            "items IS NULL OR jsonb_typeof(items) = 'array'",
            name=op.f("ck_published_messages_items_is_array"),
        ),
        sa.CheckConstraint(
            "source_message_ids IS NULL OR jsonb_typeof(source_message_ids) = 'array'",
            name=op.f("ck_published_messages_source_message_ids_is_array"),
        ),
        sa.CheckConstraint(
            "metadata_recovery = 'partial' OR ("
            "payload_hash IS NOT NULL AND random_id IS NOT NULL AND items IS NOT NULL "
            "AND source_message_ids IS NOT NULL AND series_manifest_hash IS NOT NULL)",
            name=op.f("ck_published_messages_complete_metadata"),
        ),
    )
    op.create_index(
        op.f("ix_published_messages_target_channel_id_published_at_utc"),
        TABLE,
        ["target_channel_id", "published_at_utc"],
    )
    op.create_index(op.f("ix_published_messages_window_end_utc"), TABLE, ["window_end_utc"])
    op.create_index(op.f("ix_published_messages_digest_key"), TABLE, ["digest_key"])
    op.create_index(
        "ix_published_messages_source_message_ids",
        TABLE,
        ["source_message_ids"],
        postgresql_using="gin",
        postgresql_ops={"source_message_ids": "jsonb_path_ops"},
    )


def downgrade() -> None:
    op.drop_table(TABLE)
