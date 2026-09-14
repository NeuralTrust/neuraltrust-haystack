"""Configuration, request building and safe transformation shared by the components."""

import ipaddress
import math
from copy import deepcopy
from types import TracebackType
from typing import Any, Literal, TypeVar
from urllib.parse import urlsplit

from haystack import default_from_dict, default_to_dict
from haystack.utils import Secret

from ._client import TrustGuardClient, validate_json
from .errors import NeuralTrustBlockedError, NeuralTrustInvalidResponseError

_GuardT = TypeVar("_GuardT", bound="NeuralTrustBase")
_DEFAULT_API_KEY = Secret.from_env_var("TRUSTGUARD_API_KEY")


def _api_base(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("api_base must be a string.")
    if not value or any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise ValueError("api_base must be an HTTPS origin or base path without credentials, query or fragment.")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        if (
            not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        if "?" in value or "#" in value or (port is not None and not 1 <= port <= 65535):
            raise ValueError
        loopback = hostname.lower() == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            raise ValueError
    except ValueError:
        raise ValueError("api_base requires HTTPS; HTTP is allowed only for loopback hosts.") from None
    return value.rstrip("/")


def _optional_id(value: str | None, name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{name} must be a nonempty string when provided.")


def validate_text(value: str) -> None:
    if not isinstance(value, str):
        raise TypeError("Guard content must be a string.")
    if not value.strip():
        raise ValueError("Guard content must not be empty or whitespace-only.")


def transformed_texts(payload: Any, messages: list[dict[str, Any]]) -> list[str]:
    """Map only a validated, unambiguous text transform onto the original messages."""
    invalid = "TrustGuard returned a missing, unsupported or ambiguous text transformation."
    if not isinstance(payload, dict):
        raise NeuralTrustInvalidResponseError(invalid)
    if "messages" in payload:
        transformed = payload["messages"]
        if "input" in payload or not isinstance(transformed, list) or len(transformed) != len(messages):
            raise NeuralTrustInvalidResponseError(invalid)
        result: list[str] = []
        for original, replacement in zip(messages, transformed, strict=True):
            if (
                not isinstance(replacement, dict)
                or not set(replacement).issubset({"role", "content", "name"})
                or replacement.get("role") != original["role"]
                or ("name" in replacement and replacement["name"] != original.get("name"))
            ):
                raise NeuralTrustInvalidResponseError(invalid)
            text = replacement.get("content")
            if not isinstance(text, str) or not text.strip():
                raise NeuralTrustInvalidResponseError(invalid)
            result.append(text)
        return result
    text = payload.get("input")
    if len(messages) != 1 or not isinstance(text, str) or not text.strip():
        raise NeuralTrustInvalidResponseError(invalid)
    return [text]


class NeuralTrustBase:
    """Common configuration for the text and chat guards.

    ``api_key`` is a Haystack Secret, resolved only at evaluation time. Use an
    environment-variable Secret for pipeline serialization. ``collector_key`` is
    an optional nonsecret collector selector for service-token authentication.
    Collector API keys do not need a selector.

    ``max_retries`` is the number of additional attempts (0 through 10), for
    connection failures, timeouts and HTTP 429/502/504 only. ``timeout`` is the
    HTTPX timeout in seconds for each network operation, not an overall deadline.
    ``on_violation='route'`` omits the content socket on block/ask; all evaluation
    errors still raise. No implicit approval or failure bypass is available.

    HTTP connections are pooled. Use ``with guard`` for synchronous execution or
    ``async with guard`` for async execution, or call ``close()`` / ``aclose()``
    explicitly. Each event loop owns its async pool and must close it before
    stopping. A guard can mix sync and async calls and can be reused after close.
    """

    def __init__(
        self,
        *,
        api_key: Secret = _DEFAULT_API_KEY,
        api_base: str = "https://trustguard.neuraltrust.ai",
        direction: Literal["input", "output"] = "input",
        on_violation: Literal["raise", "route"] = "raise",
        timeout: float = 5.0,
        max_retries: int = 2,
        collector_key: str | None = None,
    ) -> None:
        if not isinstance(api_key, Secret):
            raise TypeError("api_key must be a Haystack Secret.")
        if direction not in ("input", "output"):
            raise ValueError("direction must be 'input' or 'output'.")
        if on_violation not in ("raise", "route"):
            raise ValueError("on_violation must be 'raise' or 'route'.")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a finite positive number of seconds.")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 10:
            raise ValueError("max_retries must be an integer between 0 and 10.")
        _optional_id(collector_key, "collector_key")
        self.api_key = api_key
        self.api_base = _api_base(api_base)
        self.direction = direction
        self.on_violation = on_violation
        self.timeout = float(timeout)
        self.max_retries = max_retries
        self.collector_key = collector_key
        self._client = TrustGuardClient(
            api_key=self.api_key,
            endpoint=f"{self.api_base}/v1/evaluate",
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    def close(self) -> None:
        """Drain and close the synchronous pool. Later evaluations may reopen it."""
        self._client.close()

    async def aclose(self) -> None:
        """Drain this event loop's async pool and the synchronous pool.

        Call before the owning loop stops. Pools in other loops are unaffected.
        Cancellation does not interrupt cleanup; evaluations started after
        cleanup begins may create a new pool.
        """
        await self._client.aclose()

    def __enter__(self: _GuardT) -> _GuardT:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    async def __aenter__(self: _GuardT) -> _GuardT:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    def to_dict(self) -> dict[str, Any]:
        """Serialize configuration without resolving or serializing the API credential."""
        return default_to_dict(
            self,
            api_key=self.api_key.to_dict(),
            api_base=self.api_base,
            direction=self.direction,
            on_violation=self.on_violation,
            timeout=self.timeout,
            max_retries=self.max_retries,
            collector_key=self.collector_key,
        )

    @classmethod
    def from_dict(cls: type[_GuardT], data: dict[str, Any]) -> _GuardT:
        """Deserialize a component without modifying the caller's configuration dictionary."""
        copied = deepcopy(data)
        params = copied.get("init_parameters", {})
        if isinstance(params.get("api_key"), dict):
            params["api_key"] = Secret.from_dict(params["api_key"])
        return default_from_dict(cls, copied)

    def _body(
        self,
        messages: list[dict[str, Any]],
        *,
        session_id: str | None,
        consumer_id: str | None,
        attributes: dict[str, Any] | None,
    ) -> dict[str, Any]:
        _optional_id(session_id, "session_id")
        _optional_id(consumer_id, "consumer_id")
        if attributes is not None and not isinstance(attributes, dict):
            raise TypeError("attributes must be a JSON-compatible dictionary.")
        try:
            validate_json(attributes)
        except (ValueError, RecursionError):
            raise ValueError(
                "attributes must contain JSON-compatible values with string keys and finite numbers."
            ) from None
        copied_attributes = deepcopy(attributes) if attributes is not None else {}
        source = copied_attributes.setdefault("source", {})
        if not isinstance(source, dict):
            raise ValueError("attributes.source must be a JSON-compatible dictionary.")
        source.setdefault("application", "haystack")
        body: dict[str, Any] = {
            "payload": {"messages": messages},
            "direction": self.direction,
            "protocol": "llm",
            "attributes": copied_attributes,
        }
        if session_id is not None:
            body["session_id"] = session_id
        if consumer_id is not None:
            body["consumer_id"] = consumer_id
        if self.collector_key is not None:
            body["collector_key"] = self.collector_key
        return body

    def _is_blocked(self, verdict: dict[str, Any]) -> bool:
        if verdict["status"] not in ("block", "ask"):
            return False
        if self.on_violation == "raise":
            raise NeuralTrustBlockedError(verdict)
        return True
