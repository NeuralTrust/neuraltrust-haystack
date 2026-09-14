"""Evaluate a completed assistant reply with the TrustGuard output policy phase."""

import argparse

from haystack import Pipeline, component
from haystack.dataclasses import ChatMessage

from haystack_integrations.components.guardrails.neuraltrust import NeuralTrustChatGuard


@component
class ExampleAnswer:
    """Produce a fixed assistant reply locally."""

    def __init__(self, text: str) -> None:
        self.text = text

    @component.output_types(replies=list[ChatMessage])
    def run(self) -> dict[str, list[ChatMessage]]:
        """Create the completed reply that the output guard will evaluate."""
        return {"replies": [ChatMessage.from_assistant(self.text, meta={"source": "local-example"})]}


@component
class AcceptMessages:
    """Accept messages only through the guard's passing output."""

    @component.output_types(accepted=list[ChatMessage])
    def run(self, messages: list[ChatMessage]) -> dict[str, list[ChatMessage]]:
        """Receive evaluated messages through a required input socket."""
        return {"accepted": messages}


def main() -> None:
    """Run output evaluation with TRUSTGUARD_API_KEY and a local reply producer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", default="Paris is the capital of France.")
    args = parser.parse_args()

    with NeuralTrustChatGuard(direction="output", on_violation="route") as guard:
        pipeline = Pipeline()
        pipeline.add_component("answer", ExampleAnswer(args.text))
        pipeline.add_component("guard", guard)
        pipeline.add_component("accept", AcceptMessages())
        pipeline.connect("answer.replies", "guard.messages")
        pipeline.connect("guard.messages", "accept.messages")

        result = pipeline.run({}, include_outputs_from={"guard"})
    print(f"TrustGuard output verdict: {result['guard']['verdict']['status']}")
    if "accept" in result:
        for message in result["accept"]["accepted"]:
            print(f"Accepted reply: {message.text}")
    else:
        print("The protected downstream component did not run.")


if __name__ == "__main__":
    main()
