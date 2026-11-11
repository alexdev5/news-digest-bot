"""SQLAlchemy-моделі контентної БД PostgreSQL (D-022).

Тільки підтверджені частини власних публікацій. Немає таблиць pending/failed
runs, outbox, вхідних дописів чи статей (D-005). Один рядок — одне підтверджене
Telegram-повідомлення (D-002). Схема змінюється лише міграціями Alembic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


type JsonValue = Any

_COMPLETE_METADATA = (
    "metadata_recovery = 'partial' OR ("
    "payload_hash IS NOT NULL AND random_id IS NOT NULL AND items IS NOT NULL "
    "AND source_message_ids IS NOT NULL AND series_manifest_hash IS NOT NULL)"
)


class PublishedMessage(Base):
    """Підтверджена частина випуску в цільовому каналі."""

    __tablename__ = "published_messages"

    publication_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    part_key: Mapped[str] = mapped_column(Text)
    digest_key: Mapped[str] = mapped_column(Text)
    part_index: Mapped[int] = mapped_column(Integer)
    part_count: Mapped[int] = mapped_column(Integer)
    series_manifest_hash: Mapped[str | None] = mapped_column(Text)
    target_channel_id: Mapped[str] = mapped_column(Text)
    destination_peer_id: Mapped[int] = mapped_column(BigInteger)
    destination_message_id: Mapped[int] = mapped_column(BigInteger)
    window_start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmation_method: Mapped[str] = mapped_column(Text)
    rendered_text: Mapped[str] = mapped_column(Text)
    entities: Mapped[JsonValue | None] = mapped_column(JSONB)
    payload_hash: Mapped[str | None] = mapped_column(Text)
    random_id: Mapped[int | None] = mapped_column(BigInteger)
    config_version: Mapped[str | None] = mapped_column(Text)
    schedule_revision: Mapped[int | None] = mapped_column(Integer)
    source_order_snapshot: Mapped[JsonValue | None] = mapped_column(JSONB)
    items: Mapped[JsonValue | None] = mapped_column(JSONB)
    source_message_ids: Mapped[JsonValue | None] = mapped_column(JSONB)
    coverage_summary: Mapped[JsonValue | None] = mapped_column(JSONB)
    ai_usage_summary: Mapped[JsonValue | None] = mapped_column(JSONB)
    usage_scope: Mapped[str | None] = mapped_column(Text)
    metadata_recovery: Mapped[str] = mapped_column(Text)
    own_edit_history: Mapped[JsonValue | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("part_key"),
        UniqueConstraint("digest_key", "part_index"),
        # Повна назва за конвенцією перевищила б ліміт PostgreSQL у 63 символи.
        UniqueConstraint(
            "destination_peer_id",
            "destination_message_id",
            name="uq_published_messages_destination",
        ),
        CheckConstraint("part_count >= 1", name="part_count_positive"),
        CheckConstraint("part_index BETWEEN 1 AND part_count", name="part_index_range"),
        CheckConstraint("window_start_utc < window_end_utc", name="window_order"),
        CheckConstraint("confirmation_method IN ('ack', 'reconciled')", name="confirmation_method"),
        CheckConstraint("metadata_recovery IN ('complete', 'partial')", name="metadata_recovery"),
        CheckConstraint(
            "usage_scope IS NULL OR usage_scope IN ('series', 'none')", name="usage_scope"
        ),
        CheckConstraint("items IS NULL OR jsonb_typeof(items) = 'array'", name="items_is_array"),
        CheckConstraint(
            "source_message_ids IS NULL OR jsonb_typeof(source_message_ids) = 'array'",
            name="source_message_ids_is_array",
        ),
        CheckConstraint(_COMPLETE_METADATA, name="complete_metadata"),
        Index(None, "target_channel_id", "published_at_utc"),
        Index(None, "window_end_utc"),
        Index(None, "digest_key"),
        # D-012: пошук уже опублікованих джерельних ID через `@>`.
        Index(
            "ix_published_messages_source_message_ids",
            "source_message_ids",
            postgresql_using="gin",
            postgresql_ops={"source_message_ids": "jsonb_path_ops"},
        ),
    )
