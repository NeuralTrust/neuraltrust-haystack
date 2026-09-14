"""Behavior and security contracts for NeuralTrust's Haystack components."""

from __future__ import annotations

import copy
import json
import math
import ssl
from typing import Any

import httpx
import pytest
import respx
from haystack.dataclasses import ChatMessage, ImageContent, ToolCall
from haystack.utils import Secret

from haystack_integrations.components.guardrails.neuraltrust import (
    NeuralTrustAuthenticationError,
    NeuralTrustBlockedError,
    NeuralTrustChatGuard,
    NeuralTrustError,
    NeuralTrustGuard,
    NeuralTrustInvalidResponseError,
    NeuralTrustUnavailableError,
)

API_BASE = "https://trustguard.neuraltrust.ai"
API_URL = f"{API_BASE}/v1/evaluate"
API_KEY = "tgk_test_unit_secret_never_log"
TEXT = "What is the capital of France?"
FINDING = {
    "source": {"kind": "detector", "plugin": "prompt_guard"},
    "signal": {"type": "injection", "confidence": 0.98},
    "outcome": {"action": "report"},
    "evidence": {"rule": "unit-test"},
}


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTGUARD_API_KEY", API_KEY)


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def asynchronous(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


@pytest.fixture(params=["text", "chat"])
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def make_guard(kind: str, **kwargs: Any) -> NeuralTrustGuard | NeuralTrustChatGuard:
    guard_class = NeuralTrustGuard if kind == "text" else NeuralTrustChatGuard
    return guard_class(**kwargs)


def original_input(kind: str) -> str | list[ChatMessage]:
    if kind == "text":
        return TEXT
    return [ChatMessage.from_user(TEXT, meta={"nested": {"tags": ["original"]}})]


async def evaluate(
    guard: NeuralTrustGuard | NeuralTrustChatGuard,
    value: Any,
    asynchronous: bool,
    **kwargs: Any,
) -> dict[str, Any]:
    if asynchronous:
        return await guard.run_async(value, **kwargs)
    return guard.run(value, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["allow", "report", "ALLOW", "REPORT"])
async def test_passing_verdict_preserves_content_and_findings(
    respx_mock: respx.MockRouter, kind: str, asynchronous: bool, status: str
) -> None:
    response = {
        "status": status,
        "findings": [FINDING],
        "trace_id": "trace-test",
        "request_id": "request-test",
        "server_internal_field": "should not become public metadata",
    }
    route = respx_mock.post(API_URL).respond(200, json=response)
    guard = make_guard(kind)
    value = original_input(kind)
    snapshot = copy.deepcopy(value)

    result = await evaluate(guard, value, asynchronous)

    assert result[kind if kind == "text" else "messages"] == snapshot
    assert result["verdict"] == {
        "status": status.lower(),
        "findings": [FINDING],
        "trace_id": "trace-test",
        "request_id": "request-test",
    }
    assert value == snapshot
    assert route.call_count == 1
    if isinstance(value, list):
        result["messages"][0].meta["nested"]["tags"].append("changed")
        assert value == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("direction,role", [("input", "user"), ("output", "assistant")])
async def test_text_request_uses_documented_envelope(
    respx_mock: respx.MockRouter, asynchronous: bool, direction: str, role: str
) -> None:
    route = respx_mock.post(API_URL).respond(200, json={"status": "allow"})
    attributes = {"model": {"name": "example-model"}, "source": {"region": "test"}}
    original_attributes = copy.deepcopy(attributes)
    guard = NeuralTrustGuard(direction=direction, collector_key="collector-test")

    await evaluate(
        guard,
        "  preserve surrounding whitespace  ",
        asynchronous,
        session_id="session-test",
        consumer_id="consumer-test",
        attributes=attributes,
    )

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body == {
        "payload": {"messages": [{"role": role, "content": "  preserve surrounding whitespace  "}]},
        "direction": direction,
        "protocol": "llm",
        "session_id": "session-test",
        "consumer_id": "consumer-test",
        "collector_key": "collector-test",
        "attributes": {
            "model": {"name": "example-model"},
            "source": {"region": "test", "application": "haystack"},
        },
    }
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["content-type"].startswith("application/json")
    assert request.headers["user-agent"].startswith("neuraltrust-haystack/")
    assert API_KEY not in request.content.decode()
    assert attributes == original_attributes


@pytest.mark.asyncio
async def test_chat_serializes_messages_without_local_metadata(
    respx_mock: respx.MockRouter, asynchronous: bool
) -> None:
    route = respx_mock.post(API_URL).respond(200, json={"status": "allow"})
    messages = [
        ChatMessage.from_system("Be helpful", meta={"private": "local-only"}),
        ChatMessage.from_user("Hello"),
        ChatMessage.from_assistant("How can I help?", meta={"private": "local-only"}),
    ]
    await evaluate(NeuralTrustChatGuard(), messages, asynchronous)
    body = json.loads(route.calls.last.request.content)
    assert body["payload"]["messages"] == [
        {"role": "system", "content": "Be helpful"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "How can I help?"},
    ]
    assert "local-only" not in route.calls.last.request.content.decode()
    assert "collector_key" not in body
    assert "session_id" not in body
    assert "consumer_id" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["block", "ask", "BLOCK", "ASK"])
async def test_block_and_ask_raise_with_safe_metadata(
    respx_mock: respx.MockRouter, kind: str, asynchronous: bool, status: str
) -> None:
    private_evidence = "private raw detector evidence"
    respx_mock.post(API_URL).respond(
        200,
        json={
            "status": status,
            "findings": [{"evidence": {"raw": private_evidence}}],
            "trace_id": "trace-block",
            "request_id": "request-block",
        },
    )

    with pytest.raises(NeuralTrustBlockedError) as caught:
        await evaluate(make_guard(kind), original_input(kind), asynchronous)

    assert caught.value.status == status.lower()
    assert caught.value.verdict["status"] == status.lower()
    assert caught.value.verdict["trace_id"] == "trace-block"
    assert "findings" not in caught.value.verdict
    assert API_KEY not in str(caught.value)
    assert TEXT not in str(caught.value)
    assert private_evidence not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["block", "ask"])
async def test_routing_block_emits_no_safe_content_socket(
    respx_mock: respx.MockRouter, kind: str, asynchronous: bool, status: str
) -> None:
    respx_mock.post(API_URL).respond(200, json={"status": status, "findings": [FINDING]})
    value = original_input(kind)
    snapshot = copy.deepcopy(value)

    result = await evaluate(make_guard(kind, on_violation="route"), value, asynchronous)

    assert set(result) == {"verdict"}
    assert result["verdict"]["status"] == status
    assert result["verdict"]["findings"] == [FINDING]
    assert value == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{"input": "My SSN is [MASKED]"}, {"messages": [{"role": "user", "content": "My SSN is [MASKED]"}]}]
)
async def test_single_message_transform_applies_to_text_and_chat(
    respx_mock: respx.MockRouter, kind: str, asynchronous: bool, payload: dict[str, Any]
) -> None:
    respx_mock.post(API_URL).respond(200, json={"status": "transform", "transformed_payload": payload})
    original = "My SSN is 123-45-6789"
    value = original if kind == "text" else [ChatMessage.from_user(original, meta={"id": "keep-me"})]
    snapshot = copy.deepcopy(value)

    result = await evaluate(make_guard(kind), value, asynchronous)

    if kind == "text":
        assert result["text"] == "My SSN is [MASKED]"
    else:
        assert result["messages"][0].text == "My SSN is [MASKED]"
        assert result["messages"][0].meta == {"id": "keep-me"}
    assert result["verdict"]["status"] == "transform"
    assert "123-45-6789" not in str(result)
    assert value == snapshot


@pytest.mark.asyncio
async def test_chat_transform_preserves_order_roles_and_metadata(
    respx_mock: respx.MockRouter, asynchronous: bool
) -> None:
    messages = [
        ChatMessage.from_system("Be helpful", meta={"id": "system"}),
        ChatMessage.from_user("My SSN is 123-45-6789", meta={"nested": {"id": "user"}}),
    ]
    snapshot = copy.deepcopy(messages)
    respx_mock.post(API_URL).respond(
        200,
        json={
            "status": "transform",
            "transformed_payload": {
                "messages": [
                    {"role": "system", "content": "Be helpful"},
                    {"role": "user", "content": "My SSN is [MASKED]"},
                ]
            },
        },
    )

    result = await evaluate(NeuralTrustChatGuard(), messages, asynchronous)

    assert [message.text for message in result["messages"]] == ["Be helpful", "My SSN is [MASKED]"]
    assert [message.role for message in result["messages"]] == [message.role for message in messages]
    assert [message.meta for message in result["messages"]] == [message.meta for message in messages]
    result["messages"][1].meta["nested"]["id"] = "changed"
    assert messages == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"input": ""},
        {"input": "   "},
        {"input": 123},
        {"messages": []},
        {"messages": [{"role": "assistant", "content": "wrong role"}]},
        {"messages": [{"content": "missing role"}]},
        {"messages": [{"role": "user"}]},
        {"messages": [{"role": "user", "content": ""}]},
        {"messages": [{"role": "user", "content": None}]},
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "ambiguous"}]}]},
        {"messages": [{"role": "user", "content": "one"}, {"role": "user", "content": "two"}]},
        {"messages": [{"role": "user", "content": "safe", "tool_calls": [{"function": {"name": "unsafe"}}]}]},
        {"messages": [{"role": "user", "content": "safe", "function_call": {"name": "unsafe"}}]},
    ],
)
async def test_invalid_transforms_fail_closed_even_in_route_mode(
    respx_mock: respx.MockRouter, kind: str, asynchronous: bool, payload: Any
) -> None:
    route = respx_mock.post(API_URL).respond(200, json={"status": "transform", "transformed_payload": payload})
    with pytest.raises(NeuralTrustInvalidResponseError):
        await evaluate(make_guard(kind, on_violation="route"), original_input(kind), asynchronous)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_flat_transform_cannot_replace_multiple_chat_messages(
    respx_mock: respx.MockRouter, asynchronous: bool
) -> None:
    respx_mock.post(API_URL).respond(200, json={"status": "transform", "transformed_payload": {"input": "safe"}})
    messages = [ChatMessage.from_user("one"), ChatMessage.from_assistant("two")]
    with pytest.raises(NeuralTrustInvalidResponseError):
        await evaluate(NeuralTrustChatGuard(on_violation="route"), messages, asynchronous)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [None, [], "allow", {}, {"status": None}, {"status": 1}, {"status": "future_status"}, {"status": ""}]
)
async def test_malformed_and_unknown_verdicts_never_emit_content(
    respx_mock: respx.MockRouter, kind: str, asynchronous: bool, body: Any
) -> None:
    route = respx_mock.post(API_URL).respond(
        200, content=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    with pytest.raises(NeuralTrustInvalidResponseError):
        await evaluate(make_guard(kind, on_violation="route"), original_input(kind), asynchronous)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_non_json_success_fails_closed_without_retry(
    respx_mock: respx.MockRouter, kind: str, asynchronous: bool
) -> None:
    route = respx_mock.post(API_URL).respond(200, text=f"<html>{API_KEY}</html>")
    with pytest.raises(NeuralTrustInvalidResponseError) as caught:
        await evaluate(make_guard(kind, on_violation="route"), original_input(kind), asynchronous)
    assert API_KEY not in str(caught.value)
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500, 503])
async def test_http_failures_never_return_safe_content_or_retry(
    respx_mock: respx.MockRouter, kind: str, asynchronous: bool, status: int
) -> None:
    route = respx_mock.post(API_URL).respond(status, json={"error": f"sensitive body: {API_KEY} {TEXT}"})
    expected = NeuralTrustAuthenticationError if status in (401, 403) else NeuralTrustError
    with pytest.raises(expected) as caught:
        await evaluate(make_guard(kind, on_violation="route"), original_input(kind), asynchronous)
    assert route.call_count == 1
    assert API_KEY not in str(caught.value)
    assert TEXT not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 502, 504])
async def test_transient_http_failures_retry_then_pass(
    respx_mock: respx.MockRouter, asynchronous: bool, status: int
) -> None:
    route = respx_mock.post(API_URL).mock(
        side_effect=[
            httpx.Response(status, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"status": "allow"}),
        ]
    )
    result = await evaluate(NeuralTrustGuard(max_retries=1), TEXT, asynchronous)
    assert result["text"] == TEXT
    assert route.call_count == 2
    assert route.calls[0].request.content == route.calls[1].request.content


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 502, 504])
async def test_exhausted_retries_raise_unavailable(
    respx_mock: respx.MockRouter, asynchronous: bool, status: int
) -> None:
    route = respx_mock.post(API_URL).respond(status, headers={"Retry-After": "0"})
    with pytest.raises(NeuralTrustUnavailableError):
        await evaluate(NeuralTrustGuard(max_retries=2, on_violation="route"), TEXT, asynchronous)
    assert route.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout]
)
async def test_transport_unavailable_obeys_zero_retry_budget(
    respx_mock: respx.MockRouter, asynchronous: bool, error_type: type[httpx.RequestError]
) -> None:
    route = respx_mock.post(API_URL).mock(side_effect=error_type("unavailable"))
    with pytest.raises(NeuralTrustUnavailableError):
        await evaluate(NeuralTrustGuard(max_retries=0), TEXT, asynchronous)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_tls_failure_is_never_retried_or_reported_as_availability(
    respx_mock: respx.MockRouter, asynchronous: bool
) -> None:
    error = httpx.ConnectError("SSL certificate verify failed")
    route = respx_mock.post(API_URL).mock(side_effect=error)
    with pytest.raises(NeuralTrustError) as caught:
        await evaluate(NeuralTrustGuard(max_retries=2, on_violation="route"), TEXT, asynchronous)
    assert not isinstance(caught.value, NeuralTrustUnavailableError)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_tls_failure_in_exception_cause_is_never_retried(
    monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    attempts: list[None] = []

    def fail_post(*args: Any, **kwargs: Any) -> httpx.Response:
        attempts.append(None)
        raise httpx.ConnectError("connection failed") from ssl.SSLError("certificate verification failed")

    async def fail_post_async(*args: Any, **kwargs: Any) -> httpx.Response:
        return fail_post(*args, **kwargs)

    # respx wraps exception side effects with its own cause, losing the TLS chain.
    # Mock the HTTP boundary directly for this specific nested-exception behavior.
    monkeypatch.setattr(httpx.Client, "post", fail_post)
    monkeypatch.setattr(httpx.AsyncClient, "post", fail_post_async)

    with pytest.raises(NeuralTrustError) as caught:
        await evaluate(NeuralTrustGuard(max_retries=2), TEXT, asynchronous)

    assert not isinstance(caught.value, NeuralTrustUnavailableError)
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_redirect_does_not_forward_bearer_key(respx_mock: respx.MockRouter, asynchronous: bool) -> None:
    route = respx_mock.post(API_URL).respond(307, headers={"Location": "https://other.example/evaluate"})
    other_origin = respx_mock.post("https://other.example/evaluate").respond(200, json={"status": "allow"})
    with pytest.raises(NeuralTrustError):
        await evaluate(NeuralTrustGuard(), TEXT, asynchronous)
    assert route.call_count == 1
    assert not other_origin.called


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "   ", None, 123, [], {"text": "wrong type"}])
async def test_invalid_text_fails_before_network(respx_mock: respx.MockRouter, asynchronous: bool, value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        await evaluate(NeuralTrustGuard(), value, asynchronous)
    assert not respx_mock.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, [], "wrong type", [{"role": "user", "content": "wrong type"}], ["wrong type"]])
async def test_invalid_chat_fails_before_network(respx_mock: respx.MockRouter, asynchronous: bool, value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        await evaluate(NeuralTrustChatGuard(), value, asynchronous)
    assert not respx_mock.calls


def unsupported_messages() -> list[ChatMessage]:
    tool_call = ToolCall(tool_name="search", arguments={"query": "test"}, id="call-test")
    return [
        ChatMessage.from_user(""),
        ChatMessage.from_user("   "),
        ChatMessage.from_user(content_parts=["first", "second"]),
        ChatMessage.from_user(
            content_parts=[
                "Describe this image",
                ImageContent(base64_image="aW1hZ2U=", mime_type="image/png", validation=False),
            ]
        ),
        ChatMessage.from_assistant(text="Search now", tool_calls=[tool_call]),
        ChatMessage.from_tool(tool_result="Tool output", origin=tool_call),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message", unsupported_messages(), ids=["empty", "blank", "multiple-text", "image", "tool-call", "tool-result"]
)
async def test_unsupported_chat_content_cannot_bypass_inspection(
    respx_mock: respx.MockRouter, asynchronous: bool, message: ChatMessage
) -> None:
    snapshot = copy.deepcopy(message)
    with pytest.raises((ValueError, TypeError)):
        await evaluate(NeuralTrustChatGuard(), [message], asynchronous)
    assert message == snapshot
    assert not respx_mock.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_id": ""},
        {"session_id": 123},
        {"consumer_id": ""},
        {"consumer_id": []},
        {"attributes": []},
        {"attributes": {"invalid": object()}},
        {"attributes": {"invalid": math.nan}},
        {"attributes": {"invalid": math.inf}},
        {"attributes": {1: "invalid key"}},
    ],
)
async def test_invalid_attribution_fails_before_network(
    respx_mock: respx.MockRouter, kind: str, asynchronous: bool, kwargs: dict[str, Any]
) -> None:
    with pytest.raises((ValueError, TypeError)):
        await evaluate(make_guard(kind), original_input(kind), asynchronous, **kwargs)
    assert not respx_mock.calls


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_key": "raw-key"},
        {"api_key": None},
        {"direction": "both"},
        {"direction": ""},
        {"on_violation": "ignore"},
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": math.inf},
        {"timeout": math.nan},
        {"timeout": True},
        {"max_retries": -1},
        {"max_retries": 1.5},
        {"max_retries": True},
        {"collector_key": ""},
        {"collector_key": 123},
        {"api_base": ""},
        {"api_base": "ftp://example.com"},
        {"api_base": "http://remote.example"},
        {"api_base": "https://user:password@example.com"},
        {"api_base": "https://example.com?api_key=secret"},
        {"api_base": "https://example.com#fragment"},
    ],
)
def test_invalid_configuration_rejected(kind: str, kwargs: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        make_guard(kind, **kwargs)


@pytest.mark.parametrize(
    "api_base",
    [
        API_BASE,
        f"{API_BASE}/",
        "https://regional.example/trustguard",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
    ],
)
def test_valid_configurations_construct_without_credentials(
    monkeypatch: pytest.MonkeyPatch, kind: str, api_base: str
) -> None:
    monkeypatch.delenv("TRUSTGUARD_API_KEY")
    make_guard(kind, api_base=api_base)


@pytest.mark.asyncio
async def test_missing_env_secret_fails_before_network(
    monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter, kind: str, asynchronous: bool
) -> None:
    guard = make_guard(kind)
    monkeypatch.delenv("TRUSTGUARD_API_KEY")
    with pytest.raises(NeuralTrustAuthenticationError):
        await evaluate(guard, original_input(kind), asynchronous)
    assert not respx_mock.calls


def test_environment_secret_round_trip_never_serializes_key(kind: str) -> None:
    guard = make_guard(
        kind,
        api_key=Secret.from_env_var("TRUSTGUARD_API_KEY"),
        api_base="https://regional.example/trustguard/",
        direction="output",
        on_violation="route",
        timeout=12.5,
        max_retries=0,
        collector_key="collector-test",
    )
    data = guard.to_dict()
    snapshot = copy.deepcopy(data)
    restored = type(guard).from_dict(data)

    assert restored.to_dict() == snapshot
    assert data == snapshot
    assert data["init_parameters"]["api_key"] == Secret.from_env_var("TRUSTGUARD_API_KEY").to_dict()
    assert API_KEY not in json.dumps(data)


def test_token_secret_serialization_rejected(kind: str) -> None:
    guard = make_guard(kind, api_key=Secret.from_token(API_KEY))
    with pytest.raises(ValueError) as caught:
        guard.to_dict()
    assert API_KEY not in str(caught.value)


@pytest.mark.asyncio
async def test_environment_secret_resolves_at_execution_time(
    monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter, asynchronous: bool
) -> None:
    guard = NeuralTrustGuard()
    monkeypatch.setenv("TRUSTGUARD_API_KEY", "tgk_rotated_unit_key")
    route = respx_mock.post(API_URL).respond(200, json={"status": "allow"})

    await evaluate(guard, TEXT, asynchronous)

    assert route.calls.last.request.headers["authorization"] == "Bearer tgk_rotated_unit_key"


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", " ", "tgk_header\ninjected", "tgk_header\rinjected", "tgk_non_ascii_é"])
async def test_invalid_credentials_cannot_reach_transport(
    monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter, asynchronous: bool, token: str
) -> None:
    monkeypatch.setenv("TRUSTGUARD_API_KEY", token)
    with pytest.raises(NeuralTrustAuthenticationError):
        await evaluate(NeuralTrustGuard(), TEXT, asynchronous)
    assert not respx_mock.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        '{"status":"block","status":"allow"}',
        '{"status":"allow","findings":[{"confidence":NaN}]}',
        '{"status":"allow","findings":[{"confidence":Infinity}]}',
        '{"status":"allow","findings":[{"action":"block","action":"allow"}]}',
    ],
)
async def test_ambiguous_or_nonstandard_json_is_rejected(
    respx_mock: respx.MockRouter, asynchronous: bool, body: str
) -> None:
    route = respx_mock.post(API_URL).respond(200, text=body)
    with pytest.raises(NeuralTrustInvalidResponseError):
        await evaluate(NeuralTrustGuard(on_violation="route"), TEXT, asynchronous)
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"findings": "not-a-list"},
        {"findings": ["not-an-object"]},
        {"findings": None},
        {"trace_id": "trace\nlog injection"},
        {"trace_id": "x" * 257},
        {"trace_id": {}},
        {"request_id": "<script>alert(1)</script>"},
    ],
)
async def test_unusable_verdict_metadata_fails_closed(
    respx_mock: respx.MockRouter, asynchronous: bool, fields: dict[str, Any]
) -> None:
    respx_mock.post(API_URL).respond(200, json={"status": "allow", **fields})
    with pytest.raises(NeuralTrustInvalidResponseError):
        await evaluate(NeuralTrustGuard(), TEXT, asynchronous)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header,expected",
    [("0", 0), ("1.5", 1.5), ("20", 5), ("-1", 0.25), ("NaN", 0.25), ("Infinity", 0.25), ("invalid", 0.25)],
)
async def test_retry_after_is_bounded_and_finite(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock: respx.MockRouter,
    asynchronous: bool,
    header: str,
    expected: float,
) -> None:
    delays: list[float] = []

    async def record_async_delay(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("haystack_integrations.components.guardrails.neuraltrust._client.time.sleep", delays.append)
    monkeypatch.setattr(
        "haystack_integrations.components.guardrails.neuraltrust._client.asyncio.sleep", record_async_delay
    )
    route = respx_mock.post(API_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": header}),
            httpx.Response(200, json={"status": "allow"}),
        ]
    )

    result = await evaluate(NeuralTrustGuard(max_retries=1), TEXT, asynchronous)

    assert result["text"] == TEXT
    assert delays == [expected]
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_connection_failures_retry_with_same_request(
    monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter, asynchronous: bool
) -> None:
    delays: list[float] = []

    async def record_async_delay(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("haystack_integrations.components.guardrails.neuraltrust._client.time.sleep", delays.append)
    monkeypatch.setattr(
        "haystack_integrations.components.guardrails.neuraltrust._client.asyncio.sleep", record_async_delay
    )
    route = respx_mock.post(API_URL).mock(
        side_effect=[httpx.ConnectError("unavailable"), httpx.Response(200, json={"status": "allow"})]
    )

    result = await evaluate(NeuralTrustGuard(max_retries=1), TEXT, asynchronous)

    assert result["text"] == TEXT
    assert delays == [0.25]
    assert route.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.RemoteProtocolError, httpx.DecodingError, httpx.ReadError])
async def test_other_transport_failures_never_retry(
    respx_mock: respx.MockRouter, asynchronous: bool, error_type: type[httpx.RequestError]
) -> None:
    route = respx_mock.post(API_URL).mock(side_effect=error_type("failed with sensitive upstream detail"))
    with pytest.raises(NeuralTrustError) as caught:
        await evaluate(NeuralTrustGuard(max_retries=2), TEXT, asynchronous)
    assert not isinstance(caught.value, NeuralTrustUnavailableError)
    assert "sensitive upstream detail" not in str(caught.value)
    assert route.call_count == 1
