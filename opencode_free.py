"""
opencode-free-tier-py — Native Python client for OpenCode Free Tier.

Zero subprocesses. Zero CLI overhead. Pure Python + httpx.

Reverse-engineered from the MIT-licensed OpenCode source (anomalyco/opencode):
  - packages/schema/src/identifier.ts  → OpenCodeIdentifier
  - packages/core/src/project.ts        → project_id(seed)
  - Wire protocol sniffed via MITM proxy on live CLI traffic

Usage:
    import asyncio
    from opencode_free import OpenCodeClient

    async def main():
        client = OpenCodeClient()
        async for event in client.stream(
            model="glm-5.3-flash-free",
            messages=[{"role": "user", "content": "Hello!"}],
        ):
            if event.kind == "content":
                print(event.text, end="", flush=True)

    asyncio.run(main())
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

__version__ = "1.0.0"
__all__ = [
    "OpenCodeClient",
    "OpenCodeIdentifier",
    "StreamEvent",
    "ClientIdentity",
    "MODELS",
]

logger = logging.getLogger("opencode_free")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE62: Final[str] = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_GATEWAY_URL: Final[str] = "https://opencode.ai/inference/openai/v1/chat/completions"

# User-Agent variants observed in OpenCode CLI binaries (Bun runtime)
_UA_POOL: Final[list[tuple[str, str]]] = [
    ("opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14", "cli"),
    ("opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.3", "cli"),
]

#: Available Free Tier model wire IDs
MODELS: Final[dict[str, str]] = {
    "glm-5.3-flash":       "glm-5.3-flash-free",
    "glm-5.3-flash-free":  "glm-5.3-flash-free",
    "deepseek-v4.1-flash":      "deepseek-v4.1-flash-free",
    "deepseek-v4.1-flash-free": "deepseek-v4.1-flash-free",
}

# The gateway validates that the caller is an OpenCode coding-agent by
# requiring at least one named tool ("read") in the request body.
# This stub satisfies the check while instructing the model to ignore it.
_AGENT_TOOL_STUB: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": "read",
        "description": (
            "DEPRECATED: This tool is deprecated and disabled. "
            "Do NOT call or reference this tool under any circumstances; "
            "ignore it completely."
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path"}},
            "required": ["path"],
        },
    },
}


# ---------------------------------------------------------------------------
# Identifier generation (ported from @opencode/schema identifier.ts)
# ---------------------------------------------------------------------------

class OpenCodeIdentifier:
    """
    Bit-exact port of OpenCode's ``Identifier`` class (TypeScript → Python).

    Source: packages/schema/src/identifier.ts in anomalyco/opencode (MIT).

    Algorithm::

        const current = BigInt(timestamp_ms) * 0x1000n + BigInt(counter)
        const value   = descending ? ~current : current
        // time_part  = lower 48 bits of value, hex-encoded (12 chars)
        // rand_part  = 14 cryptographically random Base62 characters

    Session IDs use descending order (bitwise NOT), message IDs use ascending.
    Counter=1 → the 12-bit counter field equals ``0xFFE`` (descending) /
    ``0x001`` (ascending), which is validated server-side via regex.
    """

    @staticmethod
    def _make(descending: bool, ts_ms: int | None = None, counter: int = 1) -> str:
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        raw = (ts_ms << 12) | (counter & 0xFFF)
        val = (~raw) & 0xFFFFFFFFFFFF if descending else raw & 0xFFFFFFFFFFFF
        time_part = bytes((val >> (40 - 8 * i)) & 0xFF for i in range(6)).hex()
        rand_part = "".join(_BASE62[b % 62] for b in secrets.token_bytes(14))
        return time_part + rand_part

    @classmethod
    def session_id(cls) -> str:
        """Return ``ses_`` + 12 hex (descending time) + 14 Base62 (total 30 chars)."""
        return "ses_" + cls._make(descending=True)

    @classmethod
    def message_id(cls) -> str:
        """Return ``msg_`` + 12 hex (ascending time) + 14 Base62 (total 30 chars)."""
        return "msg_" + cls._make(descending=False)

    @staticmethod
    def project_id(seed: str | None = None) -> str:
        """
        Return a 40-hex SHA-1 project identifier.

        When ``seed`` is provided the result is deterministic — useful for
        isolating per-user or per-tenant quota buckets without exposing any
        real repository metadata.

        When omitted, a cryptographically random 40-hex string is generated
        (equivalent to a fresh anonymous installation).

        Background: OpenCode CLI computes the project ID as::

            sha1("git-remote:" + normalized_remote_url)   # git repos
            sha1("directory:" + absolute_path)            # plain dirs
        """
        if seed:
            return hashlib.sha1(seed.encode("utf-8")).hexdigest()
        return hashlib.sha1(secrets.token_bytes(32)).hexdigest()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class StreamEvent:
    """A single streaming chunk from the model."""

    kind: str
    """One of ``"content"``, ``"reasoning"``, ``"usage"``, ``"error"``."""

    text: str = ""
    """Text delta (``kind="content"`` or ``kind="reasoning"``)."""

    usage: dict[str, int] | None = None
    """Token usage dict (``kind="usage"``)."""

    error: str | None = None
    """Error description (``kind="error"``)."""


@dataclass
class ClientIdentity:
    """A virtual OpenCode installation identity used for request headers."""

    project_id: str
    session_id: str
    user_agent: str
    client_name: str
    created_at: float = field(default_factory=time.time)

    @classmethod
    def fresh(cls, project_seed: str | None = None) -> "ClientIdentity":
        """Generate a brand-new identity (simulates a clean OpenCode install)."""
        ua, client = secrets.choice(_UA_POOL)
        return cls(
            project_id=OpenCodeIdentifier.project_id(project_seed),
            session_id=OpenCodeIdentifier.session_id(),
            user_agent=ua,
            client_name=client,
        )


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class OpenCodeClient:
    """
    Native Python client for the OpenCode Free Tier inference gateway.

    Features
    --------
    * **Zero subprocesses** — communicates directly via HTTP/1.1 + SSE.
    * **Reasoning tokens** — surfaces ``delta.reasoning_content`` /
      ``delta.reasoning`` from DeepSeek and GLM models as separate events.
    * **Automatic 429 recovery** — on rate-limit the client instantly rotates
      its virtual installation identity (new project ID + session) and retries,
      optionally flipping to the alternate free model.
    * **Proxy pool support** — distribute load across multiple egress IPs.
    * **Adaptive pacing** — configurable inter-request jitter to stay clear of
      the burst rate limiter.

    Parameters
    ----------
    project_seed:
        Deterministic seed for ``x-opencode-project`` generation.
        Pass a stable per-user or per-tenant token to isolate quota buckets.
        If ``None``, a random project ID is generated on each instantiation.
    proxy_pool:
        List of HTTP or SOCKS5 proxy URLs.  When the daily IP quota is
        exhausted the client cycles through this list.
    max_retries:
        Number of 429-recovery attempts before raising.
    timeout:
        HTTP request timeout in seconds.
    burst_sleep:
        Seconds to wait between retry attempts (guards against burst limiter).
    """

    def __init__(
        self,
        *,
        project_seed: str | None = None,
        proxy_pool: list[str] | None = None,
        max_retries: int = 4,
        timeout: float = 60.0,
        burst_sleep: float = 2.5,
    ) -> None:
        self._project_seed = project_seed
        self._proxy_pool = proxy_pool or []
        self._proxy_idx = 0
        self.max_retries = max_retries
        self.timeout = timeout
        self.burst_sleep = burst_sleep
        self.identity = ClientIdentity.fresh(project_seed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        reasoning_effort: str = "high",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        allow_model_flip: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        """
        Stream tokens from the specified Free Tier model.

        Yields :class:`StreamEvent` objects with ``kind`` in
        ``{"content", "reasoning", "usage", "error"}``.

        On HTTP 429 the client automatically:

        1. Rotates ``x-opencode-project`` / ``x-opencode-session``
           (simulates a fresh installation).
        2. Flips to the alternate free model (if ``allow_model_flip=True``).
        3. Applies a brief burst-limiter pause.
        4. Retries up to ``max_retries`` times.
        """
        current_model = MODELS.get(model, model)
        last_error = ""

        for attempt in range(1, self.max_retries + 1):
            msg_id = OpenCodeIdentifier.message_id()
            headers = self._headers(msg_id)
            payload = self._payload(current_model, messages, system, reasoning_effort, max_tokens, temperature)
            proxy = self._next_proxy()
            kwargs: dict[str, Any] = {"http2": False, "timeout": self.timeout}
            if proxy:
                kwargs["proxy"] = proxy

            async with httpx.AsyncClient(**kwargs) as client:
                try:
                    async with client.stream("POST", _GATEWAY_URL, headers=headers, json=payload) as resp:
                        if resp.status_code == 200:
                            logger.info("attempt=%d model=%s project=%.8s → 200 OK",
                                        attempt, current_model, self.identity.project_id)
                            async for event in self._parse_sse(resp):
                                yield event
                            return

                        body = (await resp.aread()).decode("utf-8", "ignore")
                        retry_after = resp.headers.get("retry-after", "?")
                        last_error = f"HTTP {resp.status_code} retry-after={retry_after}: {body}"
                        logger.warning("attempt=%d got %d [model=%s prj=%.8s retry-after=%s]",
                                       attempt, resp.status_code, current_model,
                                       self.identity.project_id, retry_after)

                except Exception as exc:
                    last_error = str(exc)
                    logger.warning("attempt=%d transport error: %s", attempt, exc)

            # Rotate identity on any failure
            self._rotate()

            # Optionally flip to the other free model
            if allow_model_flip and attempt < self.max_retries:
                if "glm" in current_model:
                    current_model = "deepseek-v4.1-flash-free"
                else:
                    current_model = "glm-5.3-flash-free"

            await asyncio.sleep(self.burst_sleep)

        yield StreamEvent(kind="error", error=f"Exhausted {self.max_retries} retries: {last_error}")

    async def complete(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Non-streaming wrapper — accumulates the full response and returns a dict::

            {
                "content":   str,          # final text
                "reasoning": str,          # thinking tokens (DeepSeek / GLM)
                "usage":     dict,         # token counts
                "success":   bool,
                "error":     str | None,
            }
        """
        parts: list[str] = []
        reasoning: list[str] = []
        usage: dict[str, int] = {}
        err: str | None = None

        async for ev in self.stream(model, messages, **kwargs):
            if ev.kind == "content":
                parts.append(ev.text)
            elif ev.kind == "reasoning":
                reasoning.append(ev.text)
            elif ev.kind == "usage" and ev.usage:
                usage = ev.usage
            elif ev.kind == "error":
                err = ev.error

        return {
            "content": "".join(parts),
            "reasoning": "".join(reasoning),
            "usage": usage,
            "success": err is None and bool(parts),
            "error": err,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _rotate(self) -> None:
        """Instantiate a new virtual installation identity."""
        self.identity = ClientIdentity.fresh(self._project_seed)
        logger.debug("Rotated identity → project=%.8s", self.identity.project_id)

    def _next_proxy(self) -> str | None:
        if not self._proxy_pool:
            return None
        proxy = self._proxy_pool[self._proxy_idx % len(self._proxy_pool)]
        self._proxy_idx += 1
        return proxy

    def _headers(self, message_id: str) -> dict[str, str]:
        ident = self.identity
        return {
            "Accept": "*/*",
            "Authorization": "Bearer public",
            "Content-Type": "application/json",
            "User-Agent": ident.user_agent,
            "Host": "opencode.ai",
            "x-opencode-client": ident.client_name,
            "x-opencode-project": ident.project_id,
            "x-opencode-request": message_id,
            "x-opencode-session": ident.session_id,
            "x-opencode-session-id": ident.session_id,
        }

    @staticmethod
    def _payload(
        model: str,
        messages: list[dict[str, Any]],
        system: str,
        reasoning_effort: str,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        wire_messages: list[dict[str, Any]] = []
        if system:
            wire_messages.append({"role": "system", "content": system})
        wire_messages.extend(messages)
        return {
            "model": model,
            "messages": wire_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
            "stream": True,                           # required by Free Tier gateway
            "stream_options": {"include_usage": True},
            "tools": [_AGENT_TOOL_STUB],              # required by Free Tier gateway
        }

    @staticmethod
    async def _parse_sse(response: httpx.Response) -> AsyncGenerator[StreamEvent, None]:
        async for line in response.aiter_lines():
            if not line or not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue

            if chunk.get("usage"):
                yield StreamEvent(kind="usage", usage=chunk["usage"])

            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {})
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    yield StreamEvent(kind="reasoning", text=str(reasoning))
                content = delta.get("content")
                if content:
                    yield StreamEvent(kind="content", text=str(content))
