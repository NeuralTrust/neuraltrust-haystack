# Contributing

Development takes place in the private [NeuralTrust/neuraltrust-haystack repository](https://github.com/NeuralTrust/neuraltrust-haystack). Open feature pull requests against `develop`; `main` is the release branch. The package is not yet published to PyPI, and the Haystack registry submission remains a separate release step.

## Environment

Use Python **3.12** for the development environment. The package runtime supports Python 3.10+, while the type-checking configuration uses Python 3.12 to parse the current NumPy dependency stubs. Runtime compatibility is checked separately across Python 3.10–3.14 and both supported Haystack major versions. From the project root:

```bash
uv sync --python 3.12 --group dev
```

The package follows Haystack's component and namespace conventions, uses Hatchling for distribution builds, and keeps source under `src/haystack_integrations/components/guardrails/neuraltrust`.

## Development checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -m "not live"
```

To apply formatting:

```bash
uv run ruff format .
```

To measure branch coverage:

```bash
uv run pytest -m "not live" --cov --cov-report=term-missing
```

Hatch users can run the equivalent `hatch run lint`, `hatch run typing`, and `hatch run test -m "not live"` tasks.

## Behavior to preserve

- Keep synchronous and asynchronous signatures, output declarations, and behavior aligned. Async network calls and client/TLS initialization must not block the event loop. Preserve pooled connection reuse, per-request credential resolution and event-loop ownership; use `close()`/`aclose()` or the corresponding context managers for cleanup.
- Keep authentication, transport, malformed-response, and transformation failures closed. Route mode must not bypass errors.
- For `block` and `ask` in route mode, omit the text/messages socket. Test that a real downstream component with a required input does not execute.
- Preserve caller-owned messages, names, metadata, attributes, and serialized dictionaries. Validate transformed content before forwarding it.
- Serialize Secret policies and configuration only. Never serialize resolved credentials or runtime HTTP clients.
- Keep error messages sanitized. Findings can contain content evidence and should not be logged automatically.
- Maintain coverage for both supported Haystack major versions and test the declared minimum before changing the dependency range.

Use mocked HTTP responses for deterministic tests, including request envelopes, headers, every recognized verdict, invalid responses, and retry behavior. Actual pipeline tests should cover socket compatibility, downstream execution, and YAML round trips. Haystack 3.x provides async execution on `Pipeline.run_async`; Haystack 2.31 uses `AsyncPipeline.run_async`. Avoid replacing a pipeline test with a direct component call when the behavior being checked is routing.

## Live verification

Live verification sends supplied content to the configured TrustGuard deployment. Use credentials and synthetic fixtures intended for that purpose. Never commit API keys, personal data, credential files, or raw request/response captures.

For a manual smoke check, configure `TRUSTGUARD_API_KEY` and run:

```bash
uv run python examples/text_pipeline.py "What is the capital of France?"
uv run python examples/chat_pipeline.py
```

The first example evaluates the input phase; the second evaluates the output phase. The observed verdict depends on the collector's policy. A benign `allow` alone is insufficient to verify enforcement: also check a policy-triggering block, report, and transform, and confirm that the protected downstream component is skipped or receives transformed content as appropriate.

Keep live tests opt-in under the `live` pytest marker. Record the runtime/package versions, policy phase, expected and observed statuses, and sanitized correlation IDs. Clearly distinguish assertions verified with mocks from those observed against the service. Only claim production coverage for phases and actions exercised by the configured fixtures.

To run the opt-in production suite using a credentials file you provisioned:

```bash
uv run pytest tests/live --live \
  --trustguard-credentials /path/to/trustguard-test-credentials.json \
  --live-report .local/report.json
```

The credentials JSON contains a `collectors` object with four entries named `allow`, `block`, `report`, and `transform`. Each entry requires `api_key` and can specify `api_base` and `collector_key`. `collector_key` is a collector identifier, not the API key. The production suite requires `api_base` to be `https://trustguard.neuraltrust.ai` when supplied.

Provision the collector policies to produce the named verdict in both input and output phases for these synthetic fixtures:

| Collector entry | Fixture text | Expected behavior |
| --- | --- | --- |
| `allow` | `What is the capital of France?` | Forward unchanged. |
| `block` | `this prompt is forbidden` | Stop before the protected downstream component. |
| `report` | `this prompt is forbidden` | Forward unchanged with a report verdict. |
| `transform` | `My SSN is 123-45-6789` | Forward changed content that does not contain `123-45-6789`. |

The suite exercises text/chat components and synchronous/asynchronous pipelines in both phases. A policy mismatch fails the test. The report contains verdicts, validated correlation IDs, and findings counts rather than raw evaluated text, findings, or credentials. Keep the credentials file and generated report out of commits.

## Package checks

```bash
uv run python -m build
uv run twine check dist/*
```

Inspect both archives for unintended content and install the built wheel in a fresh environment. Run an import and pipeline serialization smoke check from outside the source directory so the checkout cannot hide a packaging error. Also verify installation from the source distribution.

Update public examples and the changelog when changing behavior. Maintain the [Haystack integration guide](https://docs.neuraltrust.ai/integrations/haystack) in the separate [NeuralTrust/docs repository](https://github.com/NeuralTrust/docs), under `integrations/haystack.mdx`. Keep research, local validation evidence, internal handoff notes and upstream registry drafts outside package commits; `.local/` is ignored for local work.


## CI and release automation

CI uses pinned `NeuralTrust/workflows` jobs for linting, the Python/Haystack test matrix, Bandit/dependency auditing and PR metadata validation. Package builds additionally check both distribution metadata and the embedded runtime version. The shared security workflow controls the enforcement policy of its scanners.

The repository uses `main` as the default release branch and `develop` for development packages:

| Trigger | Workflow | Result |
| --- | --- | --- |
| Push to `main` with `RELEASE_ENABLED=true` | `auto-release.yml` | The shared release workflow selects a semantic version, updates the changelog and version source, and creates a GitHub Release. |
| Manual Auto Release, `dry_run=true` (default) | `auto-release.yml` | Calls the shared version classifier and validates its version update hook in a temporary checkout. Creates no commit, tag, or release. |
| Published GitHub Release | `release.yml` | Checks the tag against the source and built artifacts and runs tests. Publishes the verified artifacts to PyPI only when `PYPI_PUBLISH_ENABLED=true`. |
| Manual Release | `release.yml` | Runs the same tests, builds, and artifact checks without publishing. An optional existing `release_tag` also verifies tag/source agreement. |
| Push to `develop` | `publish-dev.yml` | Stamps a unique development version in the build checkout, tests and checks it, and saves artifacts. Publishes to the development registry only when `DEV_PUBLISH_ENABLED=true`. |
| Manual Publish Dev | `publish-dev.yml` | Defaults to building and checking without publishing. `verify_auth=true` also verifies workload identity and registry upload permission; `publish=true` requests private development publication and requires `DEV_PUBLISH_ENABLED=true`. |

Documentation-only changes to README/CONTRIBUTING and workflow-only changes do not trigger publishing. Manual dispatches can validate workflow changes. Release and development checks save the verified wheel and source distribution as GitHub Actions artifacts for 14 days. The development publishing job follows the organization's GCP workload-identity flow; it performs the version stamp locally because the shared publisher does not offer a pre-build version hook.

The single version source is `src/haystack_integrations/components/guardrails/neuraltrust/_version.py`. Hatch reads it directly, so there is no duplicate project version to update in `pyproject.toml` or `uv.lock`. The shared release job invokes `python scripts/release_version.py set X.Y.Z`. Development builds use the next patch with a unique run number and attempt, for example `0.1.1.dev42+run.2`. These temporary stamps are not committed back to `develop`.

Before enabling the flows for a new repository, configure:

- Access for the repository to call the pinned `NeuralTrust/workflows` workflows.
- `OPENAI_API_KEY` and `GH_TOKEN` for auto-release, with optional `SLACK_WEBHOOK_URL`. `GH_TOKEN` must be a PAT permitted to write release commits through the branch rules; the release event created with it triggers the publishing workflow.
- The `pypi` GitHub environment and a PyPI trusted publisher for owner `NeuralTrust`, repository `neuraltrust-haystack`, workflow `release.yml`, environment `pypi`. No PyPI upload token is used.
- `DEV_GCP_PROJECT_ID` as a repository/organization variable; `DEV_WIF_PROVIDER` and `DEV_WIF_SERVICE_ACCOUNT` as secrets. The workload-identity provider must trust this repository's `develop` ref, and the service account needs write access to `europe-west1/nt-python` in the configured project.
- `RELEASE_ENABLED`, `PYPI_PUBLISH_ENABLED`, and `DEV_PUBLISH_ENABLED` are opt-in repository variables. An unset value disables the corresponding publication step. Enable each only after its publisher is configured and publication is intended.

Use these dispatches to verify the hosted workflows before publication:

```bash
gh workflow run auto-release.yml --ref main -f dry_run=true
gh workflow run release.yml --ref main
gh workflow run publish-dev.yml --ref develop -f publish=false -f verify_auth=true
```

Run the development check with `verify_auth=false` while registry identity is unconfigured. That checks the package without claiming registry authentication was verified. The authentication check uses the [Artifact Registry permissions API](https://docs.cloud.google.com/artifact-registry/docs/reference/rest/v1/projects.locations.repositories/testIamPermissions); it checks upload permission without uploading a package. Inspect the shared auto-release logs for classification warnings: its fallback to a patch bump does not prove the OpenAI classifier succeeded.

No credentials belong in workflow files. A successful release preflight proves the tested artifact build, not PyPI trusted-publisher registration or an upload. PyPI upload requires a published release event and its enable variable; manual Release dispatches cannot publish.
