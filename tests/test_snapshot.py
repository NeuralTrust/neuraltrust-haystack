"""Ensure chat results correspond to the exact content submitted for evaluation."""

import copy
import json
from typing import Any

import pytest
import respx
from haystack.dataclasses import ChatMessage
from haystack.utils import Secret

from haystack_integrations.components.guardrails.neuraltrust import NeuralTrustChatGuard
from haystack_integrations.components.guardrails.neuraltrust import chat_guard as chat_module


@pytest.mark.parametrize("status", ["allow", "report"])
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.asyncio
async def test_concurrent_input_mutation_cannot_forward_unevaluated_text(
    monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter, status: str, asynchronous: bool
) -> None:
    messages = [ChatMessage.from_user("original text")]
    mutations = 0

    def mutate_at_snapshot_boundary(value: Any) -> Any:
        nonlocal mutations
        mutations += 1
        # Deterministically model another branch replacing caller-owned input
        # immediately before the guard snapshots it, without thread scheduling.
        messages[0] = ChatMessage.from_user("text replaced by another branch")
        return copy.deepcopy(value)

    monkeypatch.setattr(chat_module, "deepcopy", mutate_at_snapshot_boundary)
    route = respx_mock.post("https://trustguard.neuraltrust.ai/v1/evaluate").respond(200, json={"status": status})
    guard = NeuralTrustChatGuard(api_key=Secret.from_token("test-credential"))
    result = await guard.run_async(messages) if asynchronous else guard.run(messages)

    evaluated_messages = json.loads(route.calls[0].request.content)["payload"]["messages"]
    assert mutations == 1
    assert result["messages"][0].text == evaluated_messages[0]["content"]
    assert result["messages"] is not messages
    assert result["messages"][0] is not messages[0]


def test_input_emptied_at_snapshot_boundary_is_rejected(
    monkeypatch: pytest.MonkeyPatch, respx_mock: respx.MockRouter
) -> None:
    messages = [ChatMessage.from_user("original text")]

    def empty_at_snapshot_boundary(value: Any) -> Any:
        messages.clear()
        return copy.deepcopy(value)

    monkeypatch.setattr(chat_module, "deepcopy", empty_at_snapshot_boundary)
    guard = NeuralTrustChatGuard(api_key=Secret.from_token("test-credential"))

    with pytest.raises(ValueError, match="messages must not be empty"):
        guard.run(messages)
    assert not respx_mock.calls
