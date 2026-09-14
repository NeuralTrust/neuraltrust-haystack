"""Evaluate input text with TrustGuard before a local pipeline sink runs."""

import argparse

from haystack import Pipeline, component

from haystack_integrations.components.guardrails.neuraltrust import NeuralTrustGuard


@component
class AcceptText:
    """Return the text supplied through the guard's passing output."""

    @component.output_types(accepted=str)
    def run(self, text: str) -> dict[str, str]:
        """Accept evaluated text through a required input socket."""
        return {"accepted": text}


def main() -> None:
    """Run a real Haystack pipeline using TRUSTGUARD_API_KEY."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", default="What is the capital of France?")
    args = parser.parse_args()

    with NeuralTrustGuard(on_violation="route") as guard:
        pipeline = Pipeline()
        pipeline.add_component("guard", guard)
        pipeline.add_component("accept", AcceptText())
        pipeline.connect("guard.text", "accept.text")

        result = pipeline.run(
            {"guard": {"text": args.text}},
            include_outputs_from={"guard"},
        )
    print(f"TrustGuard input verdict: {result['guard']['verdict']['status']}")
    if "accept" in result:
        print(f"Accepted text: {result['accept']['accepted']}")
    else:
        print("The protected downstream component did not run.")


if __name__ == "__main__":
    main()
