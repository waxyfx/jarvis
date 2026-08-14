"""Gemini, behind the :class:`AIProvider` contract.

Talks to the REST endpoint directly rather than through a vendor SDK: the
payload shape is small, stable and easy to fake in tests, and it keeps one fewer
dependency between ATLAS and a moving target.

The API key travels in the ``x-goog-api-key`` header, never in the URL. A key in
a query string ends up in proxy logs, access logs and error reports; a key in a
header does not. It is also never included in an exception message — a failing
provider must not become a way to read the credential.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from atlas_backend.ai.prompts import build_system_instruction, render_segment
from atlas_backend.ai.provider import (
    AIProviderError,
    AIRequest,
    AIResponse,
    AITimeoutError,
    FinishReason,
    MalformedResponseError,
    ProposedToolCall,
    Role,
)
from atlas_backend.config import Settings
from atlas_backend.logging import get_logger
from atlas_shared.tools.manifest import ToolDescriptor

__all__ = ["GeminiProvider", "to_function_declaration"]

log = get_logger(__name__)

#: JSON Schema keys Gemini's function declarations do not accept. Passing them
#: through produces a 400 that reads like a model error but is not one.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "$defs",
        "$ref",
        "title",
        "default",
        "additionalProperties",
        "examples",
        "format",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "prefixItems",
        # Constraint keywords Gemini's Schema type does not model. Dropping them
        # loses nothing: the authoritative validation is Pydantic's, on our side,
        # and a rejected declaration would look like a model failure when it is
        # actually a schema-compatibility problem.
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "multipleOf",
        "pattern",
    }
)


def to_function_declaration(descriptor: ToolDescriptor) -> dict[str, Any]:
    """Convert a tool manifest into a Gemini function declaration.

    The declaration is generated from the same object the Policy Engine reads,
    so the model cannot be shown a tool that policy does not know about, or a
    parameter the executor will reject.
    """
    schema = descriptor.args_schema
    cleaned = _clean_schema(schema, defs=schema.get("$defs", {}))

    # Gemini rejects an object schema with no properties; a no-argument tool is
    # declared without a parameters block instead.
    declaration: dict[str, Any] = {
        "name": descriptor.name,
        "description": _describe(descriptor),
    }
    if cleaned.get("properties"):
        declaration["parameters"] = cleaned
    return declaration


def _describe(descriptor: ToolDescriptor) -> str:
    """Summary plus the facts the model needs to choose well."""
    risk = descriptor.base_risk.value
    note = (
        " This action changes system state and may require the user to confirm it."
        if risk != "low"
        else ""
    )
    return f"{descriptor.summary} (risk: {risk}){note}"


def _clean_schema(schema: dict[str, Any], *, defs: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Pydantic JSON schema to the subset Gemini accepts."""
    if "$ref" in schema:
        reference = schema["$ref"].rsplit("/", 1)[-1]
        schema = {**defs.get(reference, {}), **{k: v for k, v in schema.items() if k != "$ref"}}

    # Optional fields arrive as anyOf[T, null]. Gemini has no union type, so the
    # non-null branch is used and the field is simply left out of `required`.
    if "anyOf" in schema:
        branches = [branch for branch in schema["anyOf"] if branch.get("type") != "null"]
        chosen = branches[0] if branches else {"type": "string"}
        merged = {**chosen}
        if "description" in schema:
            merged.setdefault("description", schema["description"])
        schema = merged

    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            cleaned[key] = {name: _clean_schema(sub, defs=defs) for name, sub in value.items()}
        elif key == "items" and isinstance(value, dict):
            cleaned[key] = _clean_schema(value, defs=defs)
        else:
            cleaned[key] = value

    cleaned.setdefault("type", "object" if "properties" in cleaned else "string")
    return cleaned


class GeminiProvider:
    name = "gemini"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        if settings.gemini_api_key is None:
            raise AIProviderError("no Gemini API key is configured")
        self._api_key = settings.gemini_api_key.get_secret_value()
        self._base_url = settings.gemini_base_url.rstrip("/")
        self._timeout = settings.ai_request_timeout_s
        self._client = client
        self._max_retries = settings.ai_max_retries
        self._retry_base_delay = settings.ai_retry_base_delay_s
        self.model = settings.gemini_model

    async def _post(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(
                url, json=payload, headers=self._headers(), timeout=self._timeout
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(url, json=payload, headers=self._headers())

    async def complete(self, request: AIRequest) -> AIResponse:
        payload = self._build_payload(request)
        url = f"{self._base_url}/models/{self.model}:generateContent"

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._post(url, payload)
            except httpx.TimeoutException as exc:
                # Not retried: a second 30-second wait is rarely what the caller
                # wants, and the turn timeout is already ticking.
                raise AITimeoutError("the model did not answer in time") from exc
            except httpx.HTTPError as exc:
                # Deliberately excludes the request: it would carry the header.
                raise AIProviderError(
                    f"could not reach the model: {type(exc).__name__}"
                ) from exc

            if response.status_code == 200:
                break

            if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                delay = _retry_delay(response, self._retry_base_delay, attempt)
                log.warning(
                    "ai_retrying",
                    status=response.status_code,
                    attempt=attempt + 1,
                    delay_s=round(delay, 1),
                )
                await asyncio.sleep(delay)
                continue

            raise AIProviderError(f"model returned HTTP {response.status_code}")

        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise MalformedResponseError("model response was not JSON") from exc

        return self._parse(body)

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

    def _build_payload(self, request: AIRequest) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        for segment in request.segments:
            contents.append(
                {
                    "role": "user" if segment.role is Role.USER else "model",
                    "parts": [{"text": render_segment(segment)}],
                }
            )

        payload: dict[str, Any] = {
            "systemInstruction": {
                "parts": [
                    {
                        "text": build_system_instruction(
                            request.language,
                            has_external_content=request.has_external_content,
                        )
                    }
                ]
            },
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_output_tokens,
            },
        }

        if request.tools:
            payload["tools"] = [
                {"functionDeclarations": [to_function_declaration(tool) for tool in request.tools]}
            ]
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        return payload

    def _parse(self, body: dict[str, Any]) -> AIResponse:
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            blocked = body.get("promptFeedback", {}).get("blockReason")
            if blocked:
                raise AIProviderError(f"the model declined to answer ({blocked})")
            raise MalformedResponseError("model response contained no candidates")

        candidate = candidates[0]
        raw_finish = str(candidate.get("finishReason", ""))
        parts = candidate.get("content", {}).get("parts", [])
        if not isinstance(parts, list):
            raise MalformedResponseError("model response had no usable parts")

        texts: list[str] = []
        calls: list[ProposedToolCall] = []
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
            function_call = part.get("functionCall")
            if isinstance(function_call, dict):
                name = function_call.get("name")
                arguments = function_call.get("args", {})
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    raise MalformedResponseError("model proposed a malformed tool call")
                calls.append(ProposedToolCall(tool=name, args=arguments, call_ref=f"c{index}"))

        usage = body.get("usageMetadata", {}) if isinstance(body.get("usageMetadata"), dict) else {}
        text = "\n".join(texts).strip()

        if calls:
            reason = FinishReason.TOOL_CALLS
        elif raw_finish == "MAX_TOKENS":
            reason = FinishReason.TRUNCATED
        elif text.endswith("?"):
            # A label for the audit trail. It carries no security meaning: a
            # text answer is a text answer either way.
            reason = FinishReason.CLARIFICATION
        else:
            reason = FinishReason.TEXT

        if reason is FinishReason.TEXT and not text and not calls:
            raise MalformedResponseError("model returned neither text nor a tool call")

        return AIResponse(
            finish_reason=reason,
            text=text,
            tool_calls=tuple(calls),
            input_tokens=_as_int(usage.get("promptTokenCount")),
            output_tokens=_as_int(usage.get("candidatesTokenCount")),
            model=self.model,
            raw_finish_reason=raw_finish,
        )


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


#: Statuses worth trying again: a rate limit or a momentarily unhealthy backend.
#: 4xx other than 429 are the caller's fault and will fail identically.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _retry_delay(response: httpx.Response, base: float, attempt: int) -> float:
    """Honour ``Retry-After`` when the server sends one; back off otherwise."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    return float(min(base * (2**attempt), 30.0))
