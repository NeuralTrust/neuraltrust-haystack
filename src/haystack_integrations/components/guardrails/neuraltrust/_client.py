"""Bounded, fail-closed synchronous and asynchronous TrustGuard transport."""

import asyncio
import json
import math
import re
import ssl
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from typing import Any

import httpx
from haystack.utils import Secret

from ._version import __version__
from .errors import (
    NeuralTrustAuthenticationError,
    NeuralTrustInvalidResponseError,
    NeuralTrustRequestError,
    NeuralTrustUnavailableError,
)

_RETRY_STATUSES = frozenset({429, 502, 504})
_STATUSES = frozenset({"allow", "report", "transform", "ask", "block"})
_CORRELATION_ID = re.compile(r"[A-Za-z0-9._:-]{1,256}\Z")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("Non-finite JSON number")


def validate_json(value: Any) -> None:
    """Validate JSON without silently coercing Python values or dictionary keys."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            validate_json(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            validate_json(item)
        return
    raise ValueError("Expected JSON-compatible values with string object keys and finite numbers.")


def parse_response(response: httpx.Response) -> tuple[dict[str, Any], Any]:
    """Return an allowlisted verdict and an untrusted transform for later validation."""
    try:
        data = json.loads(response.content, parse_constant=_invalid_constant, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise NeuralTrustInvalidResponseError("TrustGuard returned invalid JSON.") from None
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        raise NeuralTrustInvalidResponseError("TrustGuard returned a malformed verdict.")
    status = data["status"].strip().lower()
    if status not in _STATUSES:
        raise NeuralTrustInvalidResponseError("TrustGuard returned an unsupported verdict status.")
    verdict: dict[str, Any] = {"status": status}
    if "findings" in data:
        findings = data["findings"]
        if not isinstance(findings, list) or not all(isinstance(finding, dict) for finding in findings):
            raise NeuralTrustInvalidResponseError("TrustGuard returned malformed findings.")
        try:
            validate_json(findings)
        except (ValueError, RecursionError):
            raise NeuralTrustInvalidResponseError("TrustGuard returned malformed findings.") from None
        verdict["findings"] = deepcopy(findings)
    for key in ("trace_id", "request_id"):
        if key in data:
            if not isinstance(data[key], str) or _CORRELATION_ID.fullmatch(data[key]) is None:
                raise NeuralTrustInvalidResponseError("TrustGuard returned a malformed correlation ID.")
            verdict[key] = data[key]
    return verdict, data.get("transformed_payload")


def _retry_delay(attempt: int, response: httpx.Response | None = None) -> float:
    if response is not None and (header := response.headers.get("Retry-After")):
        try:
            delay = float(header)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(header)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = -1.0
        if math.isfinite(delay) and delay >= 0:
            return min(delay, 5.0)
    return float(min(0.25 * 2**attempt, 2.0))


def _tls_failure(error: BaseException) -> bool:
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        if any(hint in str(current).lower() for hint in ("certificate", "ssl", "tls")):
            return True
        current = current.__cause__ or current.__context__
    return False


def _check_status(response: httpx.Response) -> bool:
    """Return whether to retry, otherwise accept 200 or raise a sanitized error."""
    status_code = response.status_code
    if status_code == 200:
        return False
    if status_code in (401, 403):
        raise NeuralTrustAuthenticationError(
            "TrustGuard authentication or authorization failed.", status_code=status_code
        )
    if status_code in _RETRY_STATUSES:
        return True
    raise NeuralTrustRequestError("TrustGuard rejected the evaluation request.", status_code=status_code)


class _SyncPool:
    """A client generation that can drain independently of a replacement pool."""

    def __init__(self, timeout: float) -> None:
        self.client = httpx.Client(timeout=timeout, follow_redirects=False)
        self.condition = threading.Condition()
        self.active = 0

    def close(self) -> None:
        with self.condition:
            self.condition.wait_for(lambda: self.active == 0)
            if not self.client.is_closed:
                self.client.close()


async def _new_async_client(timeout: float) -> httpx.AsyncClient:
    # Client construction loads TLS certificates synchronously. An executor
    # future (rather than a second Task) survives event-loop shutdown cancellation
    # long enough to dispose of an unused client, including a late worker result.
    pending = asyncio.get_running_loop().run_in_executor(
        None, partial(httpx.AsyncClient, timeout=timeout, follow_redirects=False)
    )
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
        if not pending.cancelled() and pending.exception() is None:
            await pending.result().aclose()
        raise


class _AsyncPool:
    """All state and network operations belong to the creating event loop."""

    def __init__(self, timeout: float) -> None:
        self.initialization = asyncio.create_task(_new_async_client(timeout))
        # Retrieve failures even when all callers were cancelled before setup
        # completed; the exception is still raised to every awaiting evaluation.
        self.initialization.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        self.active = 0
        self.idle = asyncio.Event()
        self.idle.set()

    async def close(self) -> None:
        await self.idle.wait()
        try:
            client = await asyncio.shield(self.initialization)
        except Exception:
            return  # Construction failed, so there is no client to dispose of.
        await client.aclose()


class TrustGuardClient:
    """Lazy, reusable HTTP pools; async pools are owned by individual event loops.

    ``close()`` drains the synchronous pool. ``aclose()`` drains the current
    event loop's async pool and the synchronous pool. Close async pools in their
    owning loop before stopping it. Evaluations started after cleanup begins
    may create a fresh pool; cleanup never closes another loop's connections.
    """

    def __init__(self, *, api_key: Secret, endpoint: str, timeout: float, max_retries: int) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_retries = max_retries
        self._sync_lock = threading.Lock()
        self._sync_pool: _SyncPool | None = None
        self._sync_closing: set[_SyncPool] = set()
        self._async_lock = threading.Lock()
        self._async_pools: dict[asyncio.AbstractEventLoop, _AsyncPool] = {}
        self._async_closing: dict[asyncio.AbstractEventLoop, set[asyncio.Task[None]]] = {}

    def __deepcopy__(self, memo: dict[int, Any]) -> "TrustGuardClient":
        # Haystack may deepcopy components. HTTP pools, locks and event-loop
        # ownership must never cross into the copied component.
        copied = type(self)(
            api_key=deepcopy(self.api_key, memo),
            endpoint=self.endpoint,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )
        memo[id(self)] = copied
        return copied

    @contextmanager
    def _sync_http(self) -> Iterator[httpx.Client]:
        with self._sync_lock:
            if self._sync_pool is None:
                try:
                    self._sync_pool = _SyncPool(self.timeout)
                except Exception:
                    raise NeuralTrustRequestError("TrustGuard transport initialization failed.") from None
            pool = self._sync_pool
            with pool.condition:
                pool.active += 1
        try:
            yield pool.client
        finally:
            with pool.condition:
                pool.active -= 1
                pool.condition.notify_all()

    @asynccontextmanager
    async def _async_http(self) -> AsyncIterator[httpx.AsyncClient]:
        loop = asyncio.get_running_loop()
        with self._async_lock:
            pool = self._async_pools.get(loop)
            if pool is None:
                pool = _AsyncPool(self.timeout)
                self._async_pools[loop] = pool
            pool.active += 1
            pool.idle.clear()
        try:
            try:
                client = await asyncio.shield(pool.initialization)
            except Exception:
                with self._async_lock:
                    if self._async_pools.get(loop) is pool:
                        del self._async_pools[loop]
                raise NeuralTrustRequestError("TrustGuard transport initialization failed.") from None
            yield client
        finally:
            pool.active -= 1
            if not pool.active:
                pool.idle.set()

    def close(self) -> None:
        """Drain sync evaluations and close their pool; later calls may reopen it."""
        with self._sync_lock:
            if self._sync_pool is not None:
                self._sync_closing.add(self._sync_pool)
                self._sync_pool = None
            pools = tuple(self._sync_closing)
        for pool in pools:
            pool.close()
            with self._sync_lock:
                self._sync_closing.discard(pool)

    def _finished_close(self, loop: asyncio.AbstractEventLoop, task: asyncio.Task[None]) -> None:
        with self._async_lock:
            pending = self._async_closing.get(loop)
            if pending is not None:
                pending.discard(task)
                if not pending:
                    del self._async_closing[loop]
        if not task.cancelled():
            task.exception()

    async def aclose(self) -> None:
        """Drain this loop's async pool and the sync pool without blocking the loop.

        Cancellation of the caller does not cancel cleanup. Another ``aclose``
        call can await cleanup that is still running.
        """
        loop = asyncio.get_running_loop()
        with self._async_lock:
            pool = self._async_pools.pop(loop, None)
            pending = self._async_closing.setdefault(loop, set())
            if pool is not None:
                task = asyncio.create_task(pool.close())
                pending.add(task)
                task.add_done_callback(partial(self._finished_close, loop))
            sync_close = asyncio.create_task(asyncio.to_thread(self.close))
            pending.add(sync_close)
            sync_close.add_done_callback(partial(self._finished_close, loop))
            tasks = tuple(pending)
        for task in tasks:
            try:
                await asyncio.shield(task)
            finally:
                if task.done():
                    self._finished_close(loop, task)

    def _headers(self) -> dict[str, str]:
        try:
            token = self.api_key.resolve_value()
        except Exception:
            raise NeuralTrustAuthenticationError("The TrustGuard API credential could not be resolved.") from None
        if not isinstance(token, str) or not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise NeuralTrustAuthenticationError("The TrustGuard API credential is missing or invalid.")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"neuraltrust-haystack/{__version__}",
        }

    def evaluate(self, body: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        headers = self._headers()
        with self._sync_http() as client:
            for attempt in range(self.max_retries + 1):
                response = None
                try:
                    response = client.post(self.endpoint, json=body, headers=headers)
                except (httpx.TimeoutException, httpx.ConnectError) as error:
                    if _tls_failure(error):
                        raise NeuralTrustRequestError("TrustGuard TLS verification failed.") from None
                    if attempt == self.max_retries:
                        raise NeuralTrustUnavailableError("TrustGuard evaluation is unavailable.") from None
                except httpx.RequestError:
                    raise NeuralTrustRequestError("TrustGuard evaluation transport failed.") from None
                else:
                    if not _check_status(response):
                        return parse_response(response)
                    if attempt == self.max_retries:
                        raise NeuralTrustUnavailableError(
                            "TrustGuard evaluation is unavailable.", status_code=response.status_code
                        )
                time.sleep(_retry_delay(attempt, response))
        raise AssertionError("Unreachable retry state")

    async def evaluate_async(self, body: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        headers = self._headers()
        async with self._async_http() as client:
            for attempt in range(self.max_retries + 1):
                response = None
                try:
                    response = await client.post(self.endpoint, json=body, headers=headers)
                except (httpx.TimeoutException, httpx.ConnectError) as error:
                    if _tls_failure(error):
                        raise NeuralTrustRequestError("TrustGuard TLS verification failed.") from None
                    if attempt == self.max_retries:
                        raise NeuralTrustUnavailableError("TrustGuard evaluation is unavailable.") from None
                except httpx.RequestError:
                    raise NeuralTrustRequestError("TrustGuard evaluation transport failed.") from None
                else:
                    if not _check_status(response):
                        return parse_response(response)
                    if attempt == self.max_retries:
                        raise NeuralTrustUnavailableError(
                            "TrustGuard evaluation is unavailable.", status_code=response.status_code
                        )
                await asyncio.sleep(_retry_delay(attempt, response))
        raise AssertionError("Unreachable retry state")
