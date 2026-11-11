from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from newsdigest.config import ConfigError, effective_channel, load_config, parse_config_text
from newsdigest.scheduling import DailySlots, FixedInterval
from newsdigest.telegram_refs import TelegramRef, TelegramRefError, parse_telegram_ref

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "config.example.json"


def base() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "target_channels": [
            {
                "id": "news",
                "name": "Новини",
                "telegram": "@my_news_digest",
                "schedule": {"times": ["08:00", "21:00"]},
                "scan_start_at": "2026-11-12T00:00:00+02:00",
                "source_channels": [{"id": "src_a", "ref": "@source_alpha", "name": "Альфа"}],
            }
        ],
    }


def parse(data: dict[str, Any]) -> Any:
    return parse_config_text(json.dumps(data, ensure_ascii=False))


def errors_of(data: dict[str, Any] | str) -> str:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    with pytest.raises(ConfigError) as info:
        parse_config_text(text)
    return "\n".join(info.value.problems)


def channel(data: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = data["target_channels"][0]
    return result


class TestValidConfigs:
    def test_minimal(self) -> None:
        cfg = parse(base())
        eff = effective_channel(cfg, cfg.target_channels[0])
        assert eff.ai.model == "gpt-5.6-sol"
        assert eff.ai.reasoning_effort == "medium"
        assert eff.timezone.key == "Europe/Kyiv"
        assert isinstance(eff.trigger, DailySlots)
        assert eff.misfire_grace_seconds == 10_800
        assert eff.max_chars_per_message == 3500

    def test_channel_overrides_defaults(self) -> None:
        data = base()
        channel(data)["ai"] = {"model": "other-model", "prompt": "Коротко."}
        channel(data)["schedule"] = {
            "every_seconds": 21600,
            "anchor_at": "2026-11-10T00:00:00+02:00",
            "misfire_grace_seconds": 7200,
        }
        cfg = parse(data)
        eff = effective_channel(cfg, cfg.target_channels[0])
        assert eff.ai.model == "other-model"
        assert isinstance(eff.trigger, FixedInterval)
        assert eff.misfire_grace_seconds == 7200

    def test_example_file_loads(self) -> None:
        loaded = load_config(EXAMPLE)
        assert [c.id for c in loaded.config.target_channels] == ["dnipro_news", "crypto"]
        assert loaded.prompts["dnipro_news"].startswith("Тема:")
        assert loaded.prompts["crypto"]

    def test_source_order_preserved(self) -> None:
        data = base()
        channel(data)["source_channels"] = [
            {"id": "zeta", "ref": "@zeta_channel"},
            {"id": "alpha", "ref": "@alpha_channel"},
        ]
        cfg = parse(data)
        assert [s.id for s in cfg.target_channels[0].source_channels] == ["zeta", "alpha"]


class TestInvalidConfigs:
    """M27: JSON-конфіг відхиляє небезпечні або суперечливі значення."""

    def test_duplicate_json_key(self) -> None:
        text = '{"schema_version": 4, "schema_version": 4, "target_channels": []}'
        assert "повторний ключ" in errors_of(text)

    def test_nan_rejected(self) -> None:
        text = json.dumps(base()).replace('"schema_version": 4', '"schema_version": NaN')
        assert "NaN" in errors_of(text)

    def test_unknown_field(self) -> None:
        data = base()
        channel(data)["api_key"] = "secret"
        assert "api_key" in errors_of(data)

    def test_wrong_schema_version(self) -> None:
        data = base()
        data["schema_version"] = 3
        assert "schema_version" in errors_of(data)

    def test_mixed_trigger(self) -> None:
        data = base()
        channel(data)["schedule"] = {
            "times": ["08:00"],
            "every_seconds": 3600,
            "anchor_at": "2026-11-10T00:00:00+02:00",
        }
        assert "або times" in errors_of(data)

    @pytest.mark.parametrize("bad", ["8:00", "24:00", "08:60"])
    def test_bad_time(self, bad: str) -> None:
        data = base()
        channel(data)["schedule"] = {"times": [bad]}
        assert "HH:MM" in errors_of(data)

    def test_naive_scan_start(self) -> None:
        data = base()
        channel(data)["scan_start_at"] = "2026-11-12T00:00:00"
        assert "scan_start_at" in errors_of(data)

    def test_unknown_timezone(self) -> None:
        data = base()
        channel(data)["schedule"]["timezone"] = "Mars/Base"
        assert "часова зона" in errors_of(data)

    def test_target_is_own_source(self) -> None:
        data = base()
        channel(data)["source_channels"].append(
            {"id": "self", "ref": "https://t.me/My_News_Digest"}
        )
        assert "власним джерелом" in errors_of(data)

    def test_target_of_one_is_source_of_another(self) -> None:
        data = base()
        second = copy.deepcopy(channel(data))
        second.update(id="other", telegram="@other_digest")
        second["source_channels"] = [{"id": "loop", "ref": "@my_news_digest"}]
        data["target_channels"].append(second)
        assert "цикл" in errors_of(data)

    def test_duplicate_target_ids_and_targets(self) -> None:
        data = base()
        data["target_channels"].append(copy.deepcopy(channel(data)))
        problems = errors_of(data)
        assert "повторні id" in problems

    def test_duplicate_source_ids(self) -> None:
        data = base()
        channel(data)["source_sites"] = [
            {
                "id": "src_a",
                "name": "Сайт",
                "url": "https://example.com",
                "rss": "https://example.com/rss",
            }
        ]
        assert "повторні id джерел" in errors_of(data)

    def test_same_source_channel_twice(self) -> None:
        data = base()
        channel(data)["source_channels"].append({"id": "src_b", "ref": "https://t.me/Source_Alpha"})
        assert "кілька разів" in errors_of(data)

    def test_enabled_target_needs_enabled_source(self) -> None:
        data = base()
        channel(data)["source_channels"][0]["enabled"] = False
        assert "хоча б одного" in errors_of(data)

    def test_site_needs_reader(self) -> None:
        data = base()
        channel(data)["source_sites"] = [
            {"id": "site", "name": "Сайт", "url": "https://example.com"}
        ]
        assert "rss або selectors" in errors_of(data)

    @pytest.mark.parametrize(
        "url", ["ftp://example.com", "file:///etc/passwd", "https://u:p@x.com"]
    )
    def test_site_url_scheme(self, url: str) -> None:
        data = base()
        channel(data)["source_sites"] = [
            {"id": "site", "name": "Сайт", "url": url, "rss": "https://example.com/rss"}
        ]
        assert "url" in errors_of(data)

    def test_prompt_and_prompt_file(self) -> None:
        data = base()
        channel(data)["ai"] = {"prompt": "a", "prompt_file": "b.md"}
        assert "prompt_file, або prompt" in errors_of(data)

    def test_multiline_name(self) -> None:
        data = base()
        channel(data)["source_channels"][0]["name"] = "Альфа\nБета"
        assert "одним рядком" in errors_of(data)

    def test_unknown_header_field(self) -> None:
        data = base()
        channel(data)["format"] = {"header": "{name} {secret}"}
        assert "заголовок" in errors_of(data)

    def test_bad_reasoning_effort(self) -> None:
        data = base()
        channel(data)["ai"] = {"reasoning_effort": "extreme"}
        assert "reasoning_effort" in errors_of(data)


class TestPromptFiles:
    def write(self, tmp_path: Path, data: dict[str, Any]) -> Path:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_missing_prompt_file(self, tmp_path: Path) -> None:
        data = base()
        channel(data)["ai"] = {"prompt_file": "prompts/none.md"}
        with pytest.raises(ConfigError) as info:
            load_config(self.write(tmp_path, data))
        assert "prompt_file" in info.value.problems[0]

    def test_prompt_file_outside_config_dir(self, tmp_path: Path) -> None:
        (tmp_path / "outside.md").write_text("x", encoding="utf-8")
        nested = tmp_path / "cfg"
        nested.mkdir()
        data = base()
        channel(data)["ai"] = {"prompt_file": "../outside.md"}
        with pytest.raises(ConfigError) as info:
            load_config(self.write(nested, data))
        assert "всередині" in info.value.problems[0]

    def test_no_prompt_is_empty(self, tmp_path: Path) -> None:
        loaded = load_config(self.write(tmp_path, base()))
        assert loaded.prompts == {"news": ""}


class TestTelegramRefs:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("@HDnipro_news", TelegramRef("username", "hdnipro_news")),
            ("https://t.me/HDnipro_news", TelegramRef("username", "hdnipro_news")),
            ("https://t.me/HDnipro_news/", TelegramRef("username", "hdnipro_news")),
            (-1001234567890, TelegramRef("marked_id", "-1001234567890")),
            ("-1001234567890", TelegramRef("marked_id", "-1001234567890")),
        ],
    )
    def test_valid(self, raw: str | int, expected: TelegramRef) -> None:
        assert parse_telegram_ref(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "https://t.me/channel_name/123",
            "https://t.me/+AbCdEf",
            "https://t.me/joinchat/AbCd",
            "https://t.me/c/123/45",
            "http://t.me/channel_name",
            "https://example.com/channel_name",
            "@ab",
            "channel_name",
            12345,
            "",
        ],
    )
    def test_invalid(self, raw: str | int) -> None:
        with pytest.raises(TelegramRefError):
            parse_telegram_ref(raw)
