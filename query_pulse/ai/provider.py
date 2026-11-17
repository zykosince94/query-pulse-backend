"""AI providers behind one small interface: OpenAI (or a gateway that speaks its API), and a
fake one for tests and CI.

    complete(model, system, user, schema_name, schema) -> dict   one structured answer
    check(model)                                                   is the key and model usable?

OpenAI is called with plain HTTP (Chat Completions with a strict JSON schema), so the model
can only answer in the shape the server validates. A different base URL (QP_OPENAI_BASE_URL)
points the same code at an OpenAI-compatible gateway such as OpenRouter; a model there may
ignore the strict schema, so the answer is parsed and checked the same way either way.
Errors are turned into messages for the admin. They never include the key, and never the
provider's own error text, which can echo part of the key back.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Protocol
from urllib.parse import quote, urlparse

import httpx


class ProviderError(Exception):
    """The provider couldn't answer. The message is for the admin; it never holds the key."""


class AIProvider(Protocol):
    name: str

    async def complete(
        self, *, model: str, system: str, user: str, schema_name: str, schema: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def check(self, model: str) -> None: ...


PROVIDERS = {"openai": "OpenAI"}
FAKE = "fake"

OFFICIAL_HOST = "api.openai.com"
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport  # tests pass a MockTransport
        host = urlparse(self._base).hostname or "the provider"
        self._official = host == OFFICIAL_HOST
        # What the admin sees in messages: "OpenAI", or the gateway's host name.
        self._label = "OpenAI" if self._official else host

    def __repr__(self) -> str:  # never show the key, even in a traceback
        return f"OpenAIProvider(base_url={self._base!r}, api_key='***')"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._key}"},
        )

    def _error(self, response: httpx.Response, model: str) -> ProviderError:
        code = ""
        try:
            error = response.json().get("error") or {}
            code = str(error.get("code") or error.get("type") or "")
        except (ValueError, AttributeError):
            pass
        status = response.status_code
        if status == 401:
            return ProviderError(
                f"{self._label} rejected the API key. Check it, or create a new one."
            )
        if status == 403:
            return ProviderError(
                f"{self._label} refused the request: this key's project or organisation "
                f"can't use model '{model}'."
            )
        if status == 404:
            return ProviderError(
                f"{self._label} doesn't know model '{model}', or this key can't use it. "
                "Check the model name."
            )
        if status == 429:
            if code == "insufficient_quota":
                return ProviderError(
                    f"The {self._label} account has no quota left (check billing)."
                )
            return ProviderError(f"{self._label} is rate-limiting this key. Try again in a minute.")
        if status >= 500:
            return ProviderError(
                f"{self._label} had a server error (HTTP {status}). Try again later."
            )
        return ProviderError(
            f"{self._label} refused the request (HTTP {status}{f', {code}' if code else ''})."
        )

    async def _send(self, method: str, path: str, model: str, **kwargs: Any) -> httpx.Response:
        # httpx's timeout is per read, so a gateway that keeps a queued request alive by
        # trickling bytes would never trip it. The deadline covers the whole request.
        try:
            async with asyncio.timeout(self._timeout), self._client() as client:
                response = await client.request(method, f"{self._base}{path}", **kwargs)
        except (httpx.TimeoutException, TimeoutError):
            raise ProviderError(
                f"{self._label} didn't answer within {self._timeout:g} s. Try again."
            ) from None
        except httpx.HTTPError as exc:
            raise ProviderError(f"Can't reach {self._label} ({type(exc).__name__}).") from None
        if response.status_code >= 400:
            raise self._error(response, model)
        return response

    async def check(self, model: str) -> None:
        """Is the key good and the model known? Generates no text.

        OpenAI itself can retrieve one model by id. Gateways can't all do that (OpenRouter
        answers 404 for every id), so for them the key is checked at GET /key where that
        exists, and the model against the gateway's own list. The model is URL-encoded, so
        an id like "author/model:tag" stays one path segment.
        """
        if self._official:
            await self._send("GET", f"/models/{quote(model, safe='')}", model)
            return
        try:
            await self._send("GET", "/key", model)
        except ProviderError as exc:
            if "rejected the API key" in str(exc):
                raise
            # No such endpoint here, or it failed for another reason: the list decides.
        response = await self._send("GET", "/models", model)
        try:
            ids = {str(m.get("id")) for m in response.json()["data"]}
        except (ValueError, KeyError, TypeError, AttributeError):
            return  # a list we can't read: the first real request will tell
        if ids and model not in ids:
            raise ProviderError(
                f"{self._label} doesn't list model '{model}'. Check the model name."
            )

    async def complete(
        self, *, model: str, system: str, user: str, schema_name: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        response = await self._send("POST", "/chat/completions", model, json=payload)
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError(
                f"{self._label} returned a response Query Pulse couldn't read."
            ) from None
        if message.get("refusal"):
            raise ProviderError("The model declined to answer this request.")
        try:
            content = json.loads(_unfence(message.get("content") or ""))
        except ValueError:
            raise ProviderError("The model's answer wasn't valid JSON.") from None
        if not isinstance(content, dict):
            raise ProviderError("The model's answer wasn't a JSON object.")
        return content


def _unfence(text: str) -> str:
    """Some models wrap JSON in a ```json fence even when asked for plain JSON."""
    match = _FENCE.match(text)
    return match.group(1) if match else text


class FakeProvider:
    """Canned answers for tests and CI: no network, no key, no cost.

    It reads the evidence bundle out of the prompt and cites real ids from it, so a normal
    answer always passes the server's checks. `mode` makes it misbehave on purpose:
    "invalid_citation" cites an id that isn't in the bundle, "uncited" leaves a claim without
    citations, "forbidden" drafts a destructive statement.
    """

    name = FAKE

    def __init__(self, mode: str = "valid") -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    async def check(self, model: str) -> None:
        self.calls.append({"check": model})

    async def complete(
        self, *, model: str, system: str, user: str, schema_name: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append({"model": model, "schema": schema_name, "user": user})
        bundle = json.loads(user[user.index("{") :])
        ids = [item["id"] for item in bundle["items"]]
        plan = [i for i in ids if i.startswith("plan:")] or ids[:1]
        facts = [i for i in ids if i.startswith("fact:")] or ids[:1]
        rule = bundle["items"][0]["data"].get("rule_id", "the rule")
        claims = [
            {"text": f"{rule} fired on this operation.", "evidence": ["rule", "finding"]},
            {"text": "The cited plan shows where the work happens.", "evidence": plan[:2]},
            {"text": "The numbers behind it are in the facts.", "evidence": facts[:2]},
        ]
        if self.mode == "invalid_citation":
            claims[1]["evidence"] = ["plan:9.9.9"]
        if self.mode == "uncited":
            claims[2]["evidence"] = []
        summary = {"text": f"Why {rule} fired, in short.", "evidence": ["finding"]}
        if schema_name == "explanation":
            return {"summary": summary, "claims": claims, "caveats": ["Plans are estimates."]}
        engine = next(i["data"]["engine"] for i in bundle["items"] if i["id"] == "source")
        code = (
            'db.getSiblingDB("shop").getCollection("orders").createIndex({ "customerId": 1 })'
            if engine == "mongodb"
            else "CREATE INDEX CONCURRENTLY ON public.orders (customer_id);"
        )
        if self.mode == "forbidden":
            code = "DROP TABLE public.orders;"
        return {
            "summary": summary,
            "language": "mongosh" if engine == "mongodb" else "sql",
            "code": code,
            "claims": claims,
        }
