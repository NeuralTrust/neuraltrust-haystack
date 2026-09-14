"""Haystack component for unambiguous text-only ChatMessage conversations."""

from copy import deepcopy
from typing import Any

from haystack import component
from haystack.dataclasses import ChatMessage, ChatRole

from ._base import NeuralTrustBase, transformed_texts, validate_text


def _prepare_messages(messages: list[ChatMessage]) -> tuple[list[ChatMessage], list[dict[str, Any]]]:
    if not isinstance(messages, list):
        raise TypeError("messages must be a list of Haystack ChatMessage objects.")
    # Derive the request and result from the same snapshot, even if another
    # pipeline branch modifies the caller's list or messages concurrently.
    originals = deepcopy(messages)
    if not originals:
        raise ValueError("messages must not be empty.")
    payload: list[dict[str, Any]] = []
    for message in originals:
        if not isinstance(message, ChatMessage):
            raise TypeError("messages must contain only Haystack ChatMessage objects.")
        if (
            not isinstance(message.role, ChatRole)
            or message.role not in (ChatRole.SYSTEM, ChatRole.USER, ChatRole.ASSISTANT)
            or len(message) != 1
            or len(message.texts) != 1
        ):
            raise ValueError(
                "Each message must have a system, user or assistant role and exactly one text content part."
            )
        text = message.texts[0]
        validate_text(text)
        item: dict[str, Any] = {"role": message.role.value, "content": text}
        if message.name is not None:
            if not isinstance(message.name, str) or not message.name.strip():
                raise ValueError("Message names must be nonempty strings when provided.")
            item["name"] = message.name
        payload.append(item)
    return originals, payload


@component
class NeuralTrustChatGuard(NeuralTrustBase):
    """Evaluate text-only Haystack messages while preserving order, names and metadata.

    Every message must contain exactly one nonempty text part. Tools, reasoning,
    files, images, audio and multiple content parts are rejected before evaluation.
    Connect the ``messages`` output to the next component. Block and ask raise by
    default; ``on_violation='route'`` emits only ``verdict`` for these statuses.
    """

    @component.output_types(messages=list[ChatMessage], verdict=dict[str, Any])
    def run(
        self,
        messages: list[ChatMessage],
        *,
        session_id: str | None = None,
        consumer_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate a conversation and return independent copies containing only safe text."""
        originals, payload = _prepare_messages(messages)
        body = self._body(payload, session_id=session_id, consumer_id=consumer_id, attributes=attributes)
        verdict, transformed = self._client.evaluate(body)
        return self._result(originals, payload, verdict, transformed)

    @component.output_types(messages=list[ChatMessage], verdict=dict[str, Any])
    async def run_async(
        self,
        messages: list[ChatMessage],
        *,
        session_id: str | None = None,
        consumer_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate text-only messages using native async HTTP with the same enforcement as run."""
        originals, payload = _prepare_messages(messages)
        body = self._body(payload, session_id=session_id, consumer_id=consumer_id, attributes=attributes)
        verdict, transformed = await self._client.evaluate_async(body)
        return self._result(originals, payload, verdict, transformed)

    def _result(
        self,
        originals: list[ChatMessage],
        payload: list[dict[str, Any]],
        verdict: dict[str, Any],
        transformed: Any,
    ) -> dict[str, Any]:
        if self._is_blocked(verdict):
            return {"verdict": verdict}
        if verdict["status"] == "transform":
            texts = transformed_texts(transformed, payload)
            for index, (message, text) in enumerate(zip(originals, texts, strict=True)):
                serialized = deepcopy(message.to_dict())
                serialized["content"] = [{"text": text}]
                originals[index] = ChatMessage.from_dict(serialized)
        return {"messages": originals, "verdict": verdict}
