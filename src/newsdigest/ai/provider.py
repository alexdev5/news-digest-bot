"""OpenAI Responses API: один strict-JSON запит без tools і зберігання (D-007, D-011).

`store=false`, `stream=false`, без tools і `previous_response_id`; модель і
reasoning effort — з конфігу, без прихованої заміни. `store=false` не означає
Zero Data Retention.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from newsdigest.settings import SettingsError, read_secret


class AiError(RuntimeError):
    """Збій AI-проходу з машинним кодом (refusal, incomplete, invalid_json, api_error…)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AiUsage:
    model: str
    input_tokens: int
    output_tokens: int


CreateFn = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class AiSettings:
    model: str
    reasoning_effort: str
    max_output_tokens: int | None


class OpenAIProvider:
    def __init__(self, create: CreateFn, settings: AiSettings) -> None:
        self._create = create
        self._settings = settings

    @property
    def model(self) -> str:
        return self._settings.model

    async def complete_json(
        self, instructions: str, payload: dict[str, Any], schema: dict[str, Any], name: str
    ) -> tuple[dict[str, Any], AiUsage]:
        try:
            response = await self._create(
                model=self._settings.model,
                instructions=instructions,
                input=json.dumps(payload, ensure_ascii=False),
                reasoning={"effort": self._settings.reasoning_effort},
                **(
                    {"max_output_tokens": self._settings.max_output_tokens}
                    if self._settings.max_output_tokens is not None
                    else {}
                ),
                store=False,
                stream=False,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": name,
                        "schema": schema,
                        "strict": True,
                    }
                },
            )
        except Exception as exc:
            raise AiError(_api_error_code(exc), f"OpenAI: {type(exc).__name__}") from exc

        usage = _usage(response, self._settings.model)
        status = getattr(response, "status", None)
        if status != "completed":
            details = getattr(getattr(response, "incomplete_details", None), "reason", None)
            raise AiError("incomplete", f"відповідь не завершена: {status} {details or ''}".strip())
        text, refusal = _output(response)
        if refusal:
            raise AiError("refusal", "модель відмовилась відповідати")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AiError("invalid_json", "відповідь не є JSON") from exc
        if not isinstance(data, dict):
            raise AiError("invalid_json", "відповідь не є JSON-об'єктом")
        return data, usage


def _output(response: Any) -> tuple[str, bool]:
    texts: list[str] = []
    refusal = False
    for item in getattr(response, "output", None) or ():
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", None) or ():
            kind = getattr(content, "type", None)
            if kind == "output_text":
                texts.append(getattr(content, "text", ""))
            elif kind == "refusal":
                refusal = True
    return "".join(texts), refusal


def _usage(response: Any, model: str) -> AiUsage:
    usage = getattr(response, "usage", None)
    return AiUsage(
        model=str(getattr(response, "model", None) or model),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


def _api_error_code(exc: Exception) -> str:
    name = type(exc).__name__
    body = getattr(exc, "body", None)
    code = body.get("code") if isinstance(body, dict) else None
    if name == "RateLimitError":
        return "insufficient_quota" if code == "insufficient_quota" else "rate_limit"
    return {
        "AuthenticationError": "auth",
        "PermissionDeniedError": "permission",
        "NotFoundError": "model_not_found",
        "BadRequestError": "bad_request",
        "APITimeoutError": "timeout",
        "APIConnectionError": "connection",
    }.get(name, "api_error")


def openai_api_key(env: dict[str, str] | None = None) -> str:
    """`OPENAI_API_KEY(_FILE)`; порожній рядок, якщо ключ не задано."""
    import os

    values = os.environ if env is None else env
    try:
        return read_secret(values, "OPENAI_API_KEY")
    except SettingsError:
        return ""


def build_openai_provider(
    api_key: str, settings: AiSettings, timeout: float = 120.0
) -> OpenAIProvider:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=2)
    return OpenAIProvider(client.responses.create, settings)
