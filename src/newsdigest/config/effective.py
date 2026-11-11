"""Об'єднання `defaults` і налаштувань цільового каналу в готові до роботи значення."""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from newsdigest.config.models import AppConfig, ReasoningEffort, TargetChannel
from newsdigest.scheduling import DailySlots, FixedInterval, Trigger


@dataclass(frozen=True, slots=True)
class EffectiveAi:
    model: str
    reasoning_effort: ReasoningEffort
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class EffectiveChannel:
    channel: TargetChannel
    timezone: ZoneInfo
    trigger: Trigger
    misfire_grace_seconds: int
    ai: EffectiveAi
    max_chars_per_message: int
    link_preview: bool


def effective_channel(config: AppConfig, channel: TargetChannel) -> EffectiveChannel:
    defaults = config.defaults
    schedule = channel.schedule
    timezone = ZoneInfo(schedule.timezone or defaults.timezone)
    trigger: Trigger
    if schedule.times is not None:
        trigger = DailySlots(timezone=timezone, times=schedule.slot_times())
    else:
        # Валідатор Schedule гарантує обидва поля для fixed interval.
        assert schedule.every_seconds is not None and schedule.anchor_at is not None
        trigger = FixedInterval(every_seconds=schedule.every_seconds, anchor_utc=schedule.anchor_at)
    ai = EffectiveAi(
        model=channel.ai.model or defaults.ai.model,
        reasoning_effort=channel.ai.reasoning_effort or defaults.ai.reasoning_effort,
        max_output_tokens=channel.ai.max_output_tokens or defaults.ai.max_output_tokens,
    )
    grace = schedule.misfire_grace_seconds
    return EffectiveChannel(
        channel=channel,
        timezone=timezone,
        trigger=trigger,
        misfire_grace_seconds=defaults.misfire_grace_seconds if grace is None else grace,
        ai=ai,
        max_chars_per_message=defaults.publication.max_chars_per_message,
        link_preview=defaults.publication.link_preview,
    )
