"""Pydantic-моделі JSON-конфігурації schema 4 (D-021).

Модель описує структуру незалежно від сховища: зараз її читають із JSON-файлу,
надалі — з БД адмінки. Секретів у конфігу немає й бути не може (`extra="forbid"`).
"""

from __future__ import annotations

import re
from datetime import time
from string import Formatter
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from newsdigest.telegram_refs import TelegramRef, TelegramRefError, parse_telegram_ref

ItemId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_]{0,63}$")]
ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]

DEFAULT_MODEL = "gpt-6.1-terra"
DEFAULT_TIMEZONE = "Europe/Kyiv"
DEFAULT_MISFIRE_GRACE_SECONDS = 10_800
HEADER_FIELDS = frozenset({"name", "date", "date_long", "time", "period"})

_HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"невідома часова зона: {value!r}") from exc
    return value


def _validate_label(value: str) -> str:
    if not value:
        raise ValueError("назва не може бути порожньою")
    if any(ch in value for ch in "\r\n\t") or not value.isprintable():
        raise ValueError("назва має бути одним рядком без керувальних символів")
    return value


def _validate_http_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"потрібен http(s) URL з доменом: {value!r}")
    if parts.username or parts.password:
        raise ValueError("URL не може містити облікові дані")
    return value


def _validate_ref(value: str | int) -> str | int:
    try:
        parse_telegram_ref(value)
    except TelegramRefError as exc:
        raise ValueError(str(exc)) from exc
    return value


Label = Annotated[str, Field(max_length=128)]


class AiDefaults(StrictModel):
    model: str = Field(DEFAULT_MODEL, min_length=1)
    reasoning_effort: ReasoningEffort = "medium"
    max_output_tokens: int = Field(8192, ge=256, le=128_000)


class PublicationDefaults(StrictModel):
    max_chars_per_message: int = Field(3500, ge=500, le=4096)
    link_preview: bool = False


class Defaults(StrictModel):
    timezone: str = DEFAULT_TIMEZONE
    misfire_grace_seconds: int = Field(DEFAULT_MISFIRE_GRACE_SECONDS, ge=0)
    ai: AiDefaults = AiDefaults()
    publication: PublicationDefaults = PublicationDefaults()

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value: str) -> str:
        return _validate_timezone(value)


class Schedule(StrictModel):
    """Або `times` (+`timezone`), або `every_seconds` + `anchor_at`; не змішувати."""

    timezone: str | None = None
    times: tuple[str, ...] | None = None
    every_seconds: int | None = Field(None, ge=60)
    anchor_at: AwareDatetime | None = None
    misfire_grace_seconds: int | None = Field(None, ge=0)

    @field_validator("timezone")
    @classmethod
    def check_tz(cls, value: str | None) -> str | None:
        return None if value is None else _validate_timezone(value)

    @field_validator("times")
    @classmethod
    def check_times(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("times не може бути порожнім")
        for item in value:
            if not _HHMM_RE.fullmatch(item):
                raise ValueError(f"час має формат HH:MM: {item!r}")
        if len(set(value)) != len(value):
            raise ValueError("times містить повтори")
        return value

    @model_validator(mode="after")
    def check_one_trigger(self) -> Self:
        daily = self.times is not None
        interval = self.every_seconds is not None or self.anchor_at is not None
        if daily == interval:
            raise ValueError("вкажіть або times, або every_seconds разом з anchor_at")
        if interval and (self.every_seconds is None or self.anchor_at is None):
            raise ValueError("fixed interval потребує і every_seconds, і anchor_at")
        return self

    def slot_times(self) -> tuple[time, ...]:
        return tuple(time.fromisoformat(item) for item in self.times or ())


class SourceChannel(StrictModel):
    id: ItemId
    enabled: bool = True
    ref: str | int
    name: Label | None = None

    @field_validator("ref")
    @classmethod
    def check_ref(cls, value: str | int) -> str | int:
        return _validate_ref(value)

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str | None) -> str | None:
        return None if value is None else _validate_label(value)

    def parsed_ref(self) -> TelegramRef:
        return parse_telegram_ref(self.ref)


class SiteSelectors(StrictModel):
    """CSS-селектори для сайту без RSS (D-020)."""

    item: str = Field(min_length=1, description="посилання на статті на сторінці-списку")
    title: str | None = None
    date: str | None = None
    content: str | None = None


class SourceSite(StrictModel):
    id: ItemId
    enabled: bool = True
    name: Label
    url: str
    rss: str | None = None
    selectors: SiteSelectors | None = None

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return _validate_label(value)

    @field_validator("url")
    @classmethod
    def check_url(cls, value: str) -> str:
        return _validate_http_url(value)

    @field_validator("rss")
    @classmethod
    def check_rss(cls, value: str | None) -> str | None:
        return None if value is None else _validate_http_url(value)

    @model_validator(mode="after")
    def check_has_reader(self) -> Self:
        if self.rss is None and self.selectors is None:
            raise ValueError("для сайту потрібен rss або selectors")
        return self


class ChannelAi(StrictModel):
    model: str | None = Field(None, min_length=1)
    reasoning_effort: ReasoningEffort | None = None
    max_output_tokens: int | None = Field(None, ge=256, le=128_000)
    prompt_file: str | None = Field(None, min_length=1)
    prompt: str | None = Field(None, min_length=1)

    @model_validator(mode="after")
    def check_one_prompt(self) -> Self:
        if self.prompt_file is not None and self.prompt is not None:
            raise ValueError("вкажіть або prompt_file, або prompt")
        return self


class Filters(StrictModel):
    """`exclude_patterns` — підрядки без урахування регістру (не regex)."""

    exclude_patterns: tuple[Annotated[str, Field(min_length=1, max_length=200)], ...] = ()


class Format(StrictModel):
    header: str = Field("{date_long}", min_length=1, max_length=200)

    @field_validator("header")
    @classmethod
    def check_header(cls, value: str) -> str:
        try:
            fields = {name for _, name, _, _ in Formatter().parse(value) if name is not None}
        except ValueError as exc:
            raise ValueError(f"некоректний шаблон заголовка: {exc}") from exc
        unknown = fields - HEADER_FIELDS
        if unknown or "" in fields:
            allowed = ", ".join(sorted(HEADER_FIELDS))
            raise ValueError(f"заголовок дозволяє лише поля {{{allowed}}}")
        return value


class TargetChannel(StrictModel):
    """Власний канал публікації з його джерелами й правилами."""

    id: ItemId
    enabled: bool = True
    name: Label
    description: str = Field("", max_length=1000)
    telegram: str | int
    schedule: Schedule
    scan_start_at: AwareDatetime
    source_channels: tuple[SourceChannel, ...] = ()
    source_sites: tuple[SourceSite, ...] = ()
    ai: ChannelAi = ChannelAi()
    filters: Filters = Filters()
    format: Format = Format()
    daily_budget_usd: float | None = Field(None, ge=0)

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return _validate_label(value)

    @field_validator("telegram")
    @classmethod
    def check_telegram(cls, value: str | int) -> str | int:
        return _validate_ref(value)

    @model_validator(mode="after")
    def check_sources(self) -> Self:
        ids = [s.id for s in self.source_channels] + [s.id for s in self.source_sites]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"повторні id джерел: {', '.join(duplicates)}")
        refs = [s.parsed_ref() for s in self.source_channels]
        repeated = sorted({str(r) for r in refs if refs.count(r) > 1})
        if repeated:
            raise ValueError(f"один канал-джерело вказано кілька разів: {', '.join(repeated)}")
        if self.parsed_telegram() in refs:
            raise ValueError("цільовий канал не може бути власним джерелом")
        has_enabled = any(s.enabled for s in self.source_channels) or any(
            s.enabled for s in self.source_sites
        )
        if self.enabled and not has_enabled:
            raise ValueError("увімкнений цільовий канал потребує хоча б одного увімкненого джерела")
        return self

    def parsed_telegram(self) -> TelegramRef:
        return parse_telegram_ref(self.telegram)


class AppConfig(StrictModel):
    schema_version: Literal[4]
    defaults: Defaults = Defaults()
    target_channels: tuple[TargetChannel, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def check_targets(self) -> Self:
        ids = [c.id for c in self.target_channels]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"повторні id цільових каналів: {', '.join(duplicates)}")
        targets = [c.parsed_telegram() for c in self.target_channels]
        repeated = sorted({str(t) for t in targets if targets.count(t) > 1})
        if repeated:
            raise ValueError(f"цільові канали мають бути різними: {', '.join(repeated)}")
        target_set = set(targets)
        for channel in self.target_channels:
            for source in channel.source_channels:
                if source.parsed_ref() in target_set:
                    raise ValueError(
                        f"{channel.id}: джерело {source.id} є цільовим каналом — це створить цикл"
                    )
        return self

    def channel(self, channel_id: str) -> TargetChannel:
        for channel in self.target_channels:
            if channel.id == channel_id:
                return channel
        raise KeyError(channel_id)
