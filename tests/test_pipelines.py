"""Prove enforcement at real Haystack scheduling and serialization boundaries."""

import httpx
import pytest
from haystack import Pipeline, component

try:
    from haystack import AsyncPipeline
except ImportError:  # Haystack 3 unifies synchronous/asynchronous pipelines.
    AsyncPipeline = Pipeline
from haystack.core.errors import PipelineRuntimeError
from haystack.dataclasses import ChatMessage
from haystack.utils import Secret

from haystack_integrations.components.guardrails.neuraltrust import (
    NeuralTrustBlockedError,
    NeuralTrustChatGuard,
    NeuralTrustGuard,
)

URL = "https://trustguard.neuraltrust.ai/v1/evaluate"


@component
class TextSink:
    def __init__(self):
        self.seen = []

    @component.output_types(result=str)
    def run(self, text: str):
        self.seen.append(text)
        return {"result": text}


@component
class ChatSink:
    def __init__(self):
        self.seen = []

    @component.output_types(result=list[ChatMessage])
    def run(self, messages: list[ChatMessage]):
        self.seen.extend(messages)
        return {"result": messages}


@pytest.mark.parametrize("status", ["block", "ask"])
@pytest.mark.parametrize("is_async", [False, True])
async def test_route_stops_downstream(respx_mock, status, is_async):
    respx_mock.post(URL).mock(return_value=httpx.Response(200, json={"status": status}))
    pipe = AsyncPipeline() if is_async else Pipeline()
    sink = TextSink()
    pipe.add_component("guard", NeuralTrustGuard(api_key=Secret.from_token("test"), on_violation="route"))
    pipe.add_component("sink", sink)
    pipe.connect("guard.text", "sink.text")
    data = {"guard": {"text": "rejected"}}
    result = await pipe.run_async(data) if is_async else pipe.run(data)
    assert sink.seen == []
    assert "sink" not in result
    assert result["guard"]["verdict"]["status"] == status


@pytest.mark.parametrize("is_async", [False, True])
async def test_default_violation_stops_pipeline(respx_mock, is_async):
    respx_mock.post(URL).mock(return_value=httpx.Response(200, json={"status": "block"}))
    pipe = AsyncPipeline() if is_async else Pipeline()
    sink = TextSink()
    pipe.add_component("guard", NeuralTrustGuard(api_key=Secret.from_token("test")))
    pipe.add_component("sink", sink)
    pipe.connect("guard.text", "sink.text")
    with pytest.raises(PipelineRuntimeError) as caught:
        if is_async:
            await pipe.run_async({"guard": {"text": "rejected"}})
        else:
            pipe.run({"guard": {"text": "rejected"}})
    assert isinstance(caught.value.__cause__, NeuralTrustBlockedError)
    assert sink.seen == []


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("status", ["allow", "report", "transform"])
async def test_only_evaluated_content_reaches_sink(respx_mock, status, is_async):
    reply = {"status": status}
    if status == "transform":
        reply["transformed_payload"] = {"messages": [{"role": "user", "content": "sanitized"}]}
    respx_mock.post(URL).mock(return_value=httpx.Response(200, json=reply))
    pipe = AsyncPipeline() if is_async else Pipeline()
    sink = TextSink()
    pipe.add_component("guard", NeuralTrustGuard(api_key=Secret.from_token("test")))
    pipe.add_component("sink", sink)
    pipe.connect("guard.text", "sink.text")
    data = {"guard": {"text": "original"}}
    result = await pipe.run_async(data) if is_async else pipe.run(data)
    expected = "sanitized" if status == "transform" else "original"
    assert sink.seen == [expected]
    assert result["sink"]["result"] == expected


@pytest.mark.parametrize("is_async", [False, True])
async def test_chat_output_transform_preserves_metadata(respx_mock, is_async):
    route = respx_mock.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "transform",
                "transformed_payload": {"messages": [{"role": "assistant", "content": "safe"}]},
            },
        )
    )
    pipe = AsyncPipeline() if is_async else Pipeline()
    sink = ChatSink()
    pipe.add_component("guard", NeuralTrustChatGuard(api_key=Secret.from_token("test"), direction="output"))
    pipe.add_component("sink", sink)
    pipe.connect("guard.messages", "sink.messages")
    message = ChatMessage.from_assistant("original", meta={"model": "fixture"})
    data = {"guard": {"messages": [message]}}
    result = await pipe.run_async(data) if is_async else pipe.run(data)
    assert result["sink"]["result"][0].text == "safe"
    assert sink.seen[0].meta == {"model": "fixture"}
    assert message.text == "original"
    assert b'"direction":"output"' in route.calls.last.request.content


@pytest.mark.parametrize("guard_class", [NeuralTrustGuard, NeuralTrustChatGuard])
def test_pipeline_yaml_roundtrip(guard_class, monkeypatch):
    monkeypatch.setenv("TRUSTGUARD_API_KEY", "never-serialize-this")
    pipe = Pipeline()
    pipe.add_component("guard", guard_class(on_violation="route", direction="output"))
    dumped = pipe.dumps()
    assert "never-serialize-this" not in dumped
    assert "TRUSTGUARD_API_KEY" in dumped
    restored = Pipeline.loads(dumped)
    assert restored.to_dict() == pipe.to_dict()
