"""Haystack component for evaluating individual text inputs and outputs."""

from typing import Any

from haystack import component

from ._base import NeuralTrustBase, transformed_texts, validate_text


@component
class NeuralTrustGuard(NeuralTrustBase):
    """Evaluate a text string with the policy attached to a TrustGuard collector.

    Connect ``text`` to downstream components so only allowed or validated
    transformed content proceeds. The ``verdict`` output contains the status,
    findings when supplied, and correlation IDs. With ``on_violation='route'``,
    block and ask emit only ``verdict``, without a ``text`` output.

    Set ``direction='output'`` when guarding a model response; its policy phase
    and request role then become output and assistant respectively.
    """

    @component.output_types(text=str, verdict=dict[str, Any])
    def run(
        self,
        text: str,
        *,
        session_id: str | None = None,
        consumer_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate nonempty text; optional IDs and attributes provide policy and tracing context."""
        validate_text(text)
        messages = [{"role": "user" if self.direction == "input" else "assistant", "content": text}]
        body = self._body(messages, session_id=session_id, consumer_id=consumer_id, attributes=attributes)
        verdict, transformed = self._client.evaluate(body)
        return self._result(text, messages, verdict, transformed)

    @component.output_types(text=str, verdict=dict[str, Any])
    async def run_async(
        self,
        text: str,
        *,
        session_id: str | None = None,
        consumer_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate text using native async HTTP; safe to reuse across separate event loops."""
        validate_text(text)
        messages = [{"role": "user" if self.direction == "input" else "assistant", "content": text}]
        body = self._body(messages, session_id=session_id, consumer_id=consumer_id, attributes=attributes)
        verdict, transformed = await self._client.evaluate_async(body)
        return self._result(text, messages, verdict, transformed)

    def _result(
        self, text: str, messages: list[dict[str, Any]], verdict: dict[str, Any], transformed: Any
    ) -> dict[str, Any]:
        if self._is_blocked(verdict):
            return {"verdict": verdict}
        if verdict["status"] == "transform":
            text = transformed_texts(transformed, messages)[0]
        return {"text": text, "verdict": verdict}
