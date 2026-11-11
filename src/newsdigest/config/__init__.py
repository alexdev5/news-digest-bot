"""Конфігурація NewsDigest (JSON schema 4)."""

from newsdigest.config.effective import EffectiveAi, EffectiveChannel, effective_channel
from newsdigest.config.loader import ConfigError, LoadedConfig, load_config, parse_config_text
from newsdigest.config.models import AppConfig, SourceChannel, SourceSite, TargetChannel

__all__ = [
    "AppConfig",
    "ConfigError",
    "EffectiveAi",
    "EffectiveChannel",
    "LoadedConfig",
    "SourceChannel",
    "SourceSite",
    "TargetChannel",
    "effective_channel",
    "load_config",
    "parse_config_text",
]
