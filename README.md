# neuraltrust-haystack

Add [NeuralTrust TrustGuard](https://neuraltrust.ai) evaluation to Haystack text and chat pipelines. Screen user input before a model runs, or inspect completed assistant replies before returning them to your application.

Read the [official Haystack integration guide](https://docs.neuraltrust.ai/integrations/haystack) for setup and usage documentation.

## Installation

Requires Python 3.10+ and Haystack 2.31 or 3.x (`haystack-ai>=2.31.0,<4`).

```bash
pip install neuraltrust-haystack
```

For installation from source and development checks, see the [contributing guide](https://github.com/NeuralTrust/neuraltrust-haystack/blob/main/CONTRIBUTING.md).

## Connect to TrustGuard

Create or select a TrustGuard collector with the policy you want to evaluate, then set its API key in your environment:

```bash
export TRUSTGUARD_API_KEY="your-collector-api-key"
```

The default API origin is `https://trustguard.neuraltrust.ai`. Pass `api_base` for the public HTTPS origin of a regional or self-hosted deployment. The component sends evaluation requests to `/v1/evaluate`.

The API key selects the collector and its policy. The input/output direction selects the policy phase. An `allow` result only reflects the configured policy; a collector without applicable checks does not establish that content was scanned for every threat.

## Components

```python
from haystack_integrations.components.guardrails.neuraltrust import (
    NeuralTrustChatGuard,
    NeuralTrustGuard,
)
```

| Component | Required run input | Passing output |
| --- | --- | --- |
| `NeuralTrustGuard` | `text: str` | `text: str`, `verdict: dict` |
| `NeuralTrustChatGuard` | `messages: list[ChatMessage]` | `messages: list[ChatMessage]`, `verdict: dict` |

Both components implement `run`, `run_async`, `to_dict`, and `from_dict`.

### Screen text

```python
from haystack_integrations.components.guardrails.neuraltrust import NeuralTrustGuard

with NeuralTrustGuard() as guard:
    result = guard.run(text="What is the capital of France?")
    print(result["text"])
    print(result["verdict"]["status"])
```

The default `on_violation="raise"` stops execution with `NeuralTrustBlockedError` for a `block` or `ask` verdict. API and response errors also stop execution.

### Route a pipeline

Use `on_violation="route"` when the application should handle denied requests through the verdict output:

```python
from haystack import Pipeline, component

from haystack_integrations.components.guardrails.neuraltrust import NeuralTrustGuard


@component
class AcceptText:
    @component.output_types(accepted=str)
    def run(self, text: str) -> dict[str, str]:
        return {"accepted": text}


with NeuralTrustGuard(on_violation="route") as guard:
    pipeline = Pipeline()
    pipeline.add_component("guard", guard)
    pipeline.add_component("accept", AcceptText())
    pipeline.connect("guard.text", "accept.text")

    result = pipeline.run(
        {"guard": {"text": "What is the capital of France?"}},
        include_outputs_from={"guard"},
    )
    print(result["guard"]["verdict"]["status"])
    if "accept" in result:
        print(result["accept"]["accepted"])
```

On `block` or `ask`, the guard emits only `verdict`. The required `accept.text` input receives no value, so that component does not run. The guard omits the passing socket entirely: emitting an empty string or empty list would still supply a value to a downstream component. Keep guarded content connected through the guard's output, and use required inputs for the protected downstream step.

From a source checkout, the [text example](https://github.com/NeuralTrust/neuraltrust-haystack/blob/main/examples/text_pipeline.py) runs this pattern from the command line:

```bash
uv run python examples/text_pipeline.py "What is the capital of France?"
```

### Screen completed chat replies

```python
from haystack.dataclasses import ChatMessage

from haystack_integrations.components.guardrails.neuraltrust import NeuralTrustChatGuard

with NeuralTrustChatGuard(direction="output") as guard:
    result = guard.run(messages=[ChatMessage.from_assistant("Paris is the capital of France.")])
    print(result["messages"][0].text)
```

Connect `chat_generator.replies` to `guard.messages` to evaluate completed generator replies. In a source checkout, the [chat example](https://github.com/NeuralTrust/neuraltrust-haystack/blob/main/examples/chat_pipeline.py) uses a local component that produces a fixed assistant reply:

```bash
uv run python examples/chat_pipeline.py
```

The chat guard accepts a nonempty list of `system`, `user`, and `assistant` messages, each with exactly one nonempty text part, and preserves message names and metadata. Multiple content parts, reasoning, multimodal content, tool calls, and tool results are rejected. Transformed responses must map unambiguously to the original messages; incompatible message counts, roles, or content fail closed. The text guard also rejects empty or whitespace-only input.

### Async execution

```python
import asyncio

from haystack_integrations.components.guardrails.neuraltrust import NeuralTrustGuard


async def main() -> None:
    async with NeuralTrustGuard() as guard:
        result = await guard.run_async(text="What is the capital of France?")
        print(result["verdict"]["status"])


asyncio.run(main())
```

For async pipelines, Haystack 3.x uses `await Pipeline.run_async(...)`; Haystack 2.31 uses `await AsyncPipeline.run_async(...)` with `AsyncPipeline` imported from `haystack`. Synchronous and asynchronous calls use the same component inputs, verdict handling, and error behavior.

### Client lifetime

Reuse guard instances across evaluations to reuse HTTP connections. Synchronous calls share a pool; asynchronous calls use a separate pool for each event loop. Async client and TLS setup runs off the event loop and is shared by concurrent initial calls. Credentials still resolve on every evaluation.

Use `with guard` for synchronous work or `async with guard` around the lifetime of an asynchronous pipeline. At application shutdown, `guard.close()` drains the synchronous pool. `await guard.aclose()` drains both the synchronous pool and the current loop's asynchronous pool. Call it in each owning event loop before that loop stops. Cleanup is idempotent; subsequent evaluations can create a fresh pool. Network clients and locks are excluded from serialization and component copies.

## Configuration

All constructor arguments are keyword-only.

| Argument | Default | Purpose |
| --- | --- | --- |
| `api_key` | `Secret.from_env_var("TRUSTGUARD_API_KEY")` | Haystack Secret containing the evaluation credential. |
| `api_base` | `https://trustguard.neuraltrust.ai` | Public HTTPS API origin. |
| `direction` | `"input"` | Policy phase: `"input"` or `"output"`. |
| `on_violation` | `"raise"` | `"raise"` stops with an exception; `"route"` returns only the verdict for `block`/`ask`. |
| `timeout` | `5.0` | Positive HTTP timeout in seconds for each network operation, not an overall retry deadline. |
| `max_retries` | `2` | Additional attempts for eligible transient failures; integer from 0 to 10. |
| `collector_key` | `None` | Optional collector identifier when using a service token. This is not an API credential. |

The optional keyword-only run arguments `session_id`, `consumer_id`, and `attributes` attach request context. `attributes` must contain JSON-compatible values. `consumer_id` can select a policy override configured in TrustGuard.

```python
with NeuralTrustGuard() as guard:
    result = guard.run(
        text="What is the capital of France?",
        session_id="example-session",
        consumer_id="example-consumer",
        attributes={"source": {"application": "haystack-example"}},
    )
```

Configure policies in TrustGuard. These components do not accept a per-request policy ID or detector ID.

## Verdicts and errors

| TrustGuard status | Component behavior |
| --- | --- |
| `allow` | Forward original content. |
| `report` | Forward original content and return findings in `verdict`. |
| `transform` | Forward validated transformed content. |
| `block` | Raise or omit the passing output, according to `on_violation`. |
| `ask` | Stop like `block`, preserving the `ask` status. This package does not grant approval. |

The verdict contains `status` and, when provided, `findings`, `trace_id`, and `request_id`. Findings can contain sensitive evidence from evaluated content. Select the fields your application needs and apply its normal access and retention controls; avoid dumping the full verdict into logs.

Import the exceptions from the same public component namespace:

| Exception | Meaning |
| --- | --- |
| `NeuralTrustBlockedError` | `block` or `ask`; exposes `status` and a verdict limited to status and validated correlation IDs. |
| `NeuralTrustAuthenticationError` | Missing/invalid credentials or HTTP 401/403. |
| `NeuralTrustUnavailableError` | Retryable failure exhausted the configured attempts. |
| `NeuralTrustRequestError` | Rejected request or non-retryable transport failure. |
| `NeuralTrustInvalidResponseError` | Malformed verdict or unusable transformation. |
| `NeuralTrustError` | Base class for the errors above. |

Exceptions use sanitized messages. A Haystack pipeline may wrap component failures in its own execution exception; inspect the chained cause when handling a specific NeuralTrust error at the pipeline boundary.

Retries cover timeouts, connection failures, and HTTP 429/502/504. TLS failures, authentication failures, other HTTP errors, and invalid verdicts are not converted into passing content. Retry delays are bounded and honor supported `Retry-After` values up to five seconds. There is no fail-open mode; `on_violation="route"` changes only the handling of valid `block` and `ask` verdicts.

## Save and restore pipelines

```python
from haystack import Pipeline

from haystack_integrations.components.guardrails.neuraltrust import NeuralTrustGuard

pipeline = Pipeline()
pipeline.add_component("guard", NeuralTrustGuard())
serialized = pipeline.dumps()
restored = Pipeline.loads(serialized)
```

Environment-based Secrets serialize the variable name, never its resolved value. Set the credential in the restoring process before running the pipeline. `Secret.from_token(...)` is supported for direct use, but Haystack intentionally refuses to serialize token-based Secrets. The canonical `haystack_integrations` namespace also works with Haystack 3.x's default deserialization allowlist.

## Scope

- Text and text-only chat are supported. Document batches, tools, multimodal data, and native Agent lifecycle hooks are outside the components' supported interface.
- A guard before/after an Agent covers its pipeline input/output. It does not intercept the Agent's internal model calls or tool actions.
- Output evaluation happens after a completed reply. Tokens already delivered through a streaming callback cannot be withheld by a later pipeline component. Buffer replies when they must pass evaluation before delivery.
- Detection and transformation depend on the collector policy, its direction, and the TrustGuard service. Local validation does not establish detection accuracy for every policy or input.

See the [Haystack integration guide](https://docs.neuraltrust.ai/integrations/haystack) for usage documentation and the [contributing guide](https://github.com/NeuralTrust/neuraltrust-haystack/blob/main/CONTRIBUTING.md) for development checks.

Release history is recorded in the [changelog](https://github.com/NeuralTrust/neuraltrust-haystack/blob/main/CHANGELOG.md).

## License

This package is distributed under the [MIT License](https://github.com/NeuralTrust/neuraltrust-haystack/blob/main/LICENSE).
