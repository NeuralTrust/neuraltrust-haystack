"""Connection ownership, cancellation and cold-start concurrency contracts."""

from __future__ import annotations

import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from haystack.utils import Secret

from haystack_integrations.components.guardrails.neuraltrust import (
    NeuralTrustChatGuard,
    NeuralTrustGuard,
    NeuralTrustRequestError,
)
from haystack_integrations.components.guardrails.neuraltrust import _client as transport

URL = "https://trustguard.neuraltrust.ai/v1/evaluate"


@pytest.fixture
def clients(monkeypatch):
    """Track real HTTPX clients, with network traffic replaced by a yielding transport."""
    sync_clients = []
    async_clients = []
    requests = []
    original_sync = httpx.Client
    original_async = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "allow"})

    async def handle_async(request):
        await asyncio.sleep(0.001)
        return handle(request)

    def sync_factory(**kwargs):
        client = original_sync(transport=httpx.MockTransport(handle), **kwargs)
        sync_clients.append(client)
        return client

    def async_factory(**kwargs):
        client = original_async(transport=httpx.MockTransport(handle_async), **kwargs)
        async_clients.append(client)
        return client

    monkeypatch.setattr(httpx, "Client", sync_factory)
    monkeypatch.setattr(httpx, "AsyncClient", async_factory)
    monkeypatch.setenv("TRUSTGUARD_API_KEY", "test-original-key")
    return sync_clients, async_clients, requests


@pytest.mark.parametrize("guard_class", [NeuralTrustGuard, NeuralTrustChatGuard])
def test_construction_and_context_entry_are_lazy(clients, guard_class):
    with guard_class():
        assert clients[:2] == ([], [])


def test_sync_pool_reuse_rotation_and_context_cleanup(clients, monkeypatch):
    sync_clients, _, requests = clients
    guard = NeuralTrustGuard()
    with guard as entered:
        assert entered is guard
        guard.run("first")
        monkeypatch.setenv("TRUSTGUARD_API_KEY", "test-rotated-key")
        guard.run("second")
        assert len(sync_clients) == 1
        assert not sync_clients[0].is_closed
        assert "authorization" not in sync_clients[0].headers
    assert [r.headers["authorization"] for r in requests] == [
        "Bearer test-original-key",
        "Bearer test-rotated-key",
    ]
    assert sync_clients[0].is_closed
    guard.close()
    with guard:
        guard.run("reuse after close")
    assert len(sync_clients) == 2
    assert all(client.is_closed for client in sync_clients)


async def test_async_pool_reuse_rotation_and_context_cleanup(clients, monkeypatch):
    _, async_clients, requests = clients
    guard = NeuralTrustGuard()
    async with guard as entered:
        assert entered is guard
        assert not async_clients
        await guard.run_async("first")
        monkeypatch.setenv("TRUSTGUARD_API_KEY", "test-rotated-key")
        await guard.run_async("second")
        assert len(async_clients) == 1
        assert not async_clients[0].is_closed
        assert "authorization" not in async_clients[0].headers
    assert [r.headers["authorization"] for r in requests] == [
        "Bearer test-original-key",
        "Bearer test-rotated-key",
    ]
    assert async_clients[0].is_closed
    await guard.aclose()
    async with guard:
        await guard.run_async("reuse after close")
    assert len(async_clients) == 2
    assert all(client.is_closed for client in async_clients)
    assert not guard._client._async_pools
    assert not guard._client._async_closing


def test_concurrent_sync_cold_start_creates_one_pool(clients):
    guard = NeuralTrustGuard()
    with guard, ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(guard.run, ["concurrent input"] * 100))
    assert len(results) == 100
    assert len(clients[0]) == 1
    assert clients[0][0].is_closed


async def test_concurrent_cold_start_offloads_and_deduplicates_client_construction(clients, monkeypatch):
    factory = httpx.AsyncClient
    started = threading.Event()
    release = threading.Event()
    construction_threads = []

    def blocked_factory(**kwargs):
        construction_threads.append(threading.get_ident())
        started.set()
        assert release.wait(3), "The event loop did not run while client construction was blocked"
        return factory(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", blocked_factory)
    async with NeuralTrustGuard() as guard:
        tasks = [asyncio.create_task(guard.run_async("concurrent input")) for _ in range(100)]
        try:
            assert await asyncio.to_thread(started.wait, 3)
            # Progress here is deterministic evidence the loop is responsive
            # while client/TLS setup is still in progress, not a timing threshold.
            for _ in range(10):
                await asyncio.sleep(0)
            assert not any(task.done() for task in tasks)
            assert construction_threads == [construction_threads[0]]
            assert construction_threads[0] != threading.get_ident()
        finally:
            release.set()
        results = await asyncio.gather(*tasks)
        assert len(results) == 100
        assert len(clients[1]) == 1
    assert clients[1][0].is_closed


async def test_cancelling_one_initialization_waiter_does_not_cancel_other_evaluations(clients, monkeypatch):
    factory = httpx.AsyncClient
    started = threading.Event()
    release = threading.Event()

    def blocked_factory(**kwargs):
        started.set()
        assert release.wait(3)
        return factory(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", blocked_factory)
    async with NeuralTrustGuard() as guard:
        cancelled = asyncio.create_task(guard.run_async("cancel me"))
        survivor = asyncio.create_task(guard.run_async("keep me"))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            assert not survivor.done()
        finally:
            release.set()
        assert (await survivor)["text"] == "keep me"
    assert len(clients[1]) == 1
    assert clients[1][0].is_closed


async def test_cancelled_cleanup_still_disposes_late_initialization(clients, monkeypatch):
    factory = httpx.AsyncClient
    started = threading.Event()
    release = threading.Event()

    def blocked_factory(**kwargs):
        started.set()
        assert release.wait(3)
        return factory(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", blocked_factory)
    guard = NeuralTrustGuard()
    evaluation = asyncio.create_task(guard.run_async("cancel me"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        evaluation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await evaluation
        cleanup = asyncio.create_task(guard.aclose())
        await asyncio.sleep(0)
        cleanup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert not clients[1]
    finally:
        release.set()
        await guard.aclose()
    assert len(clients[1]) == 1
    assert clients[1][0].is_closed
    assert not guard._client._async_pools
    assert not guard._client._async_closing


async def test_cancelling_initializer_disposes_late_worker_result(clients, monkeypatch):
    factory = httpx.AsyncClient
    started = threading.Event()
    release = threading.Event()

    def blocked_factory(**kwargs):
        started.set()
        assert release.wait(3)
        return factory(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", blocked_factory)
    initialization = asyncio.create_task(transport._new_async_client(5))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        initialization.cancel()
        await asyncio.sleep(0)
        initialization.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await initialization
    assert clients[1][0].is_closed


async def test_async_cleanup_drains_existing_evaluation_and_allows_fresh_pool(clients, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    factory = httpx.AsyncClient

    def custom_factory(**kwargs):
        client = factory(**kwargs)
        if len(clients[1]) == 1:
            original_post = client.post

            async def blocked_post(*args, **kwargs):
                started.set()
                await release.wait()
                assert not client.is_closed
                return await original_post(*args, **kwargs)

            client.post = blocked_post
        return client

    monkeypatch.setattr(httpx, "AsyncClient", custom_factory)
    guard = NeuralTrustGuard()
    evaluation = asyncio.create_task(guard.run_async("in flight"))
    await started.wait()
    cleanup = asyncio.create_task(guard.aclose())
    try:
        await asyncio.sleep(0)
        assert not cleanup.done()
        assert not clients[1][0].is_closed
        assert (await guard.run_async("new generation"))["text"] == "new generation"
        assert len(clients[1]) == 2
    finally:
        release.set()
    assert (await evaluation)["text"] == "in flight"
    await cleanup
    assert clients[1][0].is_closed
    assert not clients[1][1].is_closed
    await guard.aclose()
    assert clients[1][1].is_closed


def test_sync_cleanup_drains_inflight_evaluation(clients, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    closing = threading.Event()
    original_close = transport._SyncPool.close
    factory = httpx.Client

    def custom_factory(**kwargs):
        client = factory(**kwargs)
        original_post = client.post

        def blocked_post(*args, **kwargs):
            started.set()
            assert release.wait(3)
            assert not client.is_closed
            return original_post(*args, **kwargs)

        client.post = blocked_post
        return client

    def observe_close(pool):
        closing.set()
        original_close(pool)

    monkeypatch.setattr(httpx, "Client", custom_factory)
    monkeypatch.setattr(transport._SyncPool, "close", observe_close)
    guard = NeuralTrustGuard()
    with ThreadPoolExecutor(max_workers=2) as executor:
        evaluation = executor.submit(guard.run, "in flight")
        try:
            assert started.wait(3)
            cleanup = executor.submit(guard.close)
            assert closing.wait(3)
            assert not cleanup.done()
            assert not clients[0][0].is_closed
        finally:
            release.set()
        assert evaluation.result(timeout=3)["text"] == "in flight"
        cleanup.result(timeout=3)
    assert clients[0][0].is_closed


async def test_sync_close_leaves_async_pool_and_aclose_closes_both(clients):
    guard = NeuralTrustGuard()
    guard.run("sync")
    await guard.run_async("async")
    guard.close()
    assert clients[0][0].is_closed
    assert not clients[1][0].is_closed
    guard.run("sync again")
    await guard.aclose()
    assert all(client.is_closed for client in clients[0] + clients[1])


def test_pools_are_owned_and_closed_by_independent_event_loops(clients, monkeypatch):
    owner_threads = {}
    factory = httpx.AsyncClient
    rendezvous = threading.Barrier(2)

    def custom_factory(**kwargs):
        client = factory(**kwargs)
        original_post = client.post
        original_close = client.aclose

        async def checked_post(*args, **kwargs):
            owner = owner_threads.setdefault(id(client), threading.get_ident())
            assert owner == threading.get_ident()
            return await original_post(*args, **kwargs)

        async def checked_close():
            assert owner_threads[id(client)] == threading.get_ident()
            await original_close()

        client.post = checked_post
        client.aclose = checked_close
        return client

    monkeypatch.setattr(httpx, "AsyncClient", custom_factory)
    guard = NeuralTrustGuard()

    async def use_guard():
        async with guard:
            await guard.run_async("first")
            await asyncio.to_thread(rendezvous.wait, 3)
            await guard.run_async("second")

    def run_loop():
        asyncio.run(use_guard())

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_loop) for _ in range(2)]
        for future in futures:
            future.result(timeout=5)
    assert len(clients[1]) == 2
    assert len(set(owner_threads.values())) == 2
    assert all(client.is_closed for client in clients[1])
    assert not guard._client._async_pools
    assert not guard._client._async_closing


def test_new_event_loop_never_reuses_previous_loop_pool(clients):
    guard = NeuralTrustGuard()

    async def use_guard():
        async with guard:
            await guard.run_async("loop-local input")

    asyncio.run(use_guard())
    asyncio.run(use_guard())
    assert len(clients[1]) == 2
    assert all(client.is_closed for client in clients[1])


@pytest.mark.parametrize("guard_class", [NeuralTrustGuard, NeuralTrustChatGuard])
async def test_deepcopy_and_serialization_keep_only_transport_configuration(clients, guard_class):
    guard = guard_class(api_key=Secret.from_env_var("TRUSTGUARD_API_KEY"))
    configuration = guard.to_dict()
    # Exercise transport directly so this contract covers both guard classes.
    guard._client.evaluate({})
    await guard._client.evaluate_async({})
    copied = copy.deepcopy(guard)
    assert copied.to_dict() == configuration == guard.to_dict()
    assert copied._client._sync_pool is None
    assert not copied._client._async_pools
    assert copied.api_key is copied._client.api_key
    async with copied:
        copied._client.evaluate({})
        await copied._client.evaluate_async({})
    assert len(clients[0]) == len(clients[1]) == 2
    assert not clients[0][0].is_closed
    assert not clients[1][0].is_closed
    await guard.aclose()
    assert all(client.is_closed for client in clients[0] + clients[1])


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_initialization_failures_are_sanitized_and_can_recover(clients, monkeypatch, asynchronous):
    name = "AsyncClient" if asynchronous else "Client"
    original = getattr(httpx, name)

    def fail(**kwargs: Any):
        raise RuntimeError("sensitive-proxy-or-local-certificate-path")

    monkeypatch.setattr(httpx, name, fail)
    async with NeuralTrustGuard() as guard:
        with pytest.raises(NeuralTrustRequestError, match="transport initialization failed") as caught:
            if asynchronous:
                await guard.run_async("first")
            else:
                guard.run("first")
        assert "sensitive" not in str(caught.value)
        monkeypatch.setattr(httpx, name, original)
        result = await guard.run_async("retry") if asynchronous else guard.run("retry")
        assert result["text"] == "retry"


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_context_cleanup_on_exception(clients, asynchronous):
    with pytest.raises(ValueError, match="application failure"):
        if asynchronous:
            async with NeuralTrustGuard() as guard:
                await guard.run_async("input")
                raise ValueError("application failure")
        else:
            with NeuralTrustGuard() as guard:
                guard.run("input")
                raise ValueError("application failure")
    assert all(client.is_closed for client in clients[0] + clients[1])
