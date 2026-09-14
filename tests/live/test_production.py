"""Opt-in production contract and pipeline verification; fixtures remain external."""

import pytest
from haystack import Pipeline, component

try:
    from haystack import AsyncPipeline
except ImportError:  # Haystack 3 unifies synchronous/asynchronous pipelines.
    AsyncPipeline = Pipeline
from haystack.dataclasses import ChatMessage
from haystack.utils import Secret

from haystack_integrations.components.guardrails.neuraltrust import NeuralTrustChatGuard, NeuralTrustGuard

pytestmark = pytest.mark.live
INPUTS = {
    "allow": "What is the capital of France?",
    "block": "this prompt is forbidden",
    "report": "this prompt is forbidden",
    "transform": "My SSN is 123-45-6789",
}


@component
class ProductionTextSink:
    def __init__(self):
        self.seen = []

    @component.output_types(result=str)
    def run(self, text: str):
        self.seen.append(text)
        return {"result": text}


@component
class ProductionChatSink:
    def __init__(self):
        self.seen = []

    @component.output_types(result=list[ChatMessage])
    def run(self, messages: list[ChatMessage]):
        self.seen.extend(messages)
        return {"result": messages}


@pytest.mark.parametrize("expected", ["allow", "block", "report", "transform"])
@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("chat", [False, True], ids=["text", "chat"])
@pytest.mark.parametrize("direction", ["input", "output"])
async def test_production_pipeline(live_collectors, live_evidence, expected, is_async, chat, direction):
    entry = live_collectors[expected]
    api_base = entry.get("api_base") or "https://trustguard.neuraltrust.ai"
    assert api_base.rstrip("/") == "https://trustguard.neuraltrust.ai", "Production fixture endpoint mismatch"
    guard_class = NeuralTrustChatGuard if chat else NeuralTrustGuard
    async with guard_class(
        api_key=Secret.from_token(entry["api_key"]),
        api_base=api_base,
        collector_key=entry.get("collector_key"),
        direction=direction,
        on_violation="route",
        timeout=15,
        max_retries=1,
    ) as guard:
        pipe = AsyncPipeline() if is_async else Pipeline()
        sink = ProductionChatSink() if chat else ProductionTextSink()
        pipe.add_component("guard", guard)
        pipe.add_component("sink", sink)
        socket = "messages" if chat else "text"
        pipe.connect(f"guard.{socket}", f"sink.{socket}")
        original = INPUTS[expected]
        message_factory = ChatMessage.from_user if direction == "input" else ChatMessage.from_assistant
        value = [message_factory(original, meta={"verification": "neuraltrust-haystack"})] if chat else original
        data = {"guard": {socket: value, "session_id": "neuraltrust-haystack-local-verification"}}
        result = await pipe.run_async(data) if is_async else pipe.run(data)
        verdict = result["guard"]["verdict"]
        evidence = {
            "component": guard_class.__name__,
            "execution": "async" if is_async else "sync",
            "direction": direction,
            "expected": expected,
            "actual": verdict["status"],
            "trace_id": verdict.get("trace_id"),
            "request_id": verdict.get("request_id"),
            "findings_count": len(verdict.get("findings", [])),
            "passed": False,
        }
        live_evidence.append(evidence)
        assert verdict["status"] == expected, "Production policy returned an unexpected verdict"
        if expected == "block":
            assert sink.seen == []
            assert "sink" not in result
            assert socket not in result["guard"]
        else:
            assert sink.seen
            forwarded = sink.seen[0].text if chat else sink.seen[0]
            if expected == "transform":
                assert forwarded != original
                assert "123-45-6789" not in forwarded
            else:
                assert forwarded == original
            if chat:
                assert sink.seen[0].meta == {"verification": "neuraltrust-haystack"}
                assert value[0].text == original
        evidence["passed"] = True


async def test_production_reused_sync_and_async_pools(live_collectors, live_evidence):
    """Exercise repeated production evaluations through both pools of one guard."""
    entry = live_collectors["allow"]
    api_base = entry.get("api_base") or "https://trustguard.neuraltrust.ai"
    assert api_base.rstrip("/") == "https://trustguard.neuraltrust.ai", "Production fixture endpoint mismatch"
    async with NeuralTrustGuard(
        api_key=Secret.from_token(entry["api_key"]),
        api_base=api_base,
        collector_key=entry.get("collector_key"),
        timeout=15,
        max_retries=1,
    ) as guard:
        for is_async in (False, True):
            for iteration in (1, 2):
                parameters = {
                    "text": INPUTS["allow"],
                    "session_id": "neuraltrust-haystack-local-pool-verification",
                }
                result = await guard.run_async(**parameters) if is_async else guard.run(**parameters)
                verdict = result["verdict"]
                evidence = {
                    "component": "NeuralTrustGuard",
                    "verification": "pooled-client-reuse",
                    "execution": "async" if is_async else "sync",
                    "direction": "input",
                    "iteration": iteration,
                    "expected": "allow",
                    "actual": verdict["status"],
                    "trace_id": verdict.get("trace_id"),
                    "request_id": verdict.get("request_id"),
                    "findings_count": len(verdict.get("findings", [])),
                    "passed": False,
                }
                live_evidence.append(evidence)
                assert verdict["status"] == "allow", "Production policy returned an unexpected verdict"
                assert result["text"] == INPUTS["allow"]
                evidence["passed"] = True
