# Contributing

Development takes place in the [NeuralTrust/neuraltrust-haystack repository](https://github.com/NeuralTrust/neuraltrust-haystack). Open feature pull requests against `develop`; `main` is the release branch.

## Environment

Use Python **3.12** for the development environment. The package runtime supports Python 3.10+, while the type-checking configuration uses Python 3.12 to parse the NumPy dependency stubs. Runtime compatibility is checked separately across Python 3.10–3.14 and both supported Haystack major versions. Clone the repository and install with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/NeuralTrust/neuraltrust-haystack.git
cd neuraltrust-haystack
uv sync --python 3.12 --group dev
```

For an editable installation with pip, run `python -m pip install -e .` from the checkout.

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

Include release notes for user-visible changes and significant development or release-workflow changes in each PR, under `## [Unreleased]` in `CHANGELOG.md`. For a manually prepared release, move those notes into `## [vX.Y.Z] — YYYY-MM-DD` and set the matching package version before merging. For automatically prepared releases, the shared workflow performs that promotion and leaves an empty Unreleased section for subsequent changes.

## CI and release automation

CI uses pinned `NeuralTrust/workflows` jobs for linting, the Python/Haystack test matrix, Bandit/dependency auditing and PR metadata validation. Package builds additionally check both distribution metadata and the embedded runtime version. The shared security workflow controls the enforcement policy of its scanners.

Full CI runs on pull requests and pushes to `main`. The shared Python workflow runs lint once and tests all ten Python/Haystack combinations sequentially in isolated environments, with a results table in the job summary. Every combination is attempted, and any failure fails the check. PR title and description edits run only metadata validation. Development publication performs its own tests on pushes to `develop`.

The repository uses `main` as the default release branch and `develop` for development packages:

| Trigger | Workflow | Result |
| --- | --- | --- |
| Push to `main` after the first published stable release | `auto-release.yml` | The shared release workflow selects a semantic version, updates the changelog and version source, and creates a GitHub Release. |
| Manual Auto Release, `dry_run=true` (default) | `auto-release.yml` | Calls the shared version classifier and validates its version update hook in a temporary checkout. Creates no commit, tag, or release. |
| Manual Auto Release on `main`, `dry_run=false` | `auto-release.yml` | Uses the shared workflow to create a versioned GitHub Release, including the initial release. |
| Published GitHub Release | `release.yml` | Checks the tag against the source and built artifacts, runs tests, and automatically publishes the verified artifacts to PyPI using trusted publishing. |
| Manual Release | `release.yml` | Runs the same tests, builds, and artifact checks without publishing. An optional existing `release_tag` also verifies tag/source agreement. |
| Push to `develop` | `publish-dev.yml` | Stamps a unique development version in the build checkout, tests and checks it, and saves artifacts. Publishes to the development registry only when `DEV_PUBLISH_ENABLED=true`. |
| Manual Publish Dev | `publish-dev.yml` | Defaults to building and checking without publishing. `verify_auth=true` also verifies workload identity and registry upload permission; `publish=true` requests private development publication and requires `DEV_PUBLISH_ENABLED=true`. |

Documentation-only changes to README/CONTRIBUTING and workflow-only changes do not trigger publishing. Manual dispatches can validate workflow changes. Release and development checks save the verified wheel and source distribution as GitHub Actions artifacts for 14 days. The development publishing job follows the organization's GCP workload-identity flow; it performs the version stamp locally because the shared publisher does not offer a pre-build version hook.

The single version source is `src/haystack_integrations/components/guardrails/neuraltrust/_version.py`. Hatch reads it directly, so there is no duplicate project version to update in `pyproject.toml` or `uv.lock`. The shared release job invokes `python scripts/release_version.py set X.Y.Z`. Development builds use the next patch with a unique run number and attempt, for example `0.1.1.dev42+run.2`. These temporary stamps are not committed back to `develop`.

To publish a manually prepared version, merge its version and changelog changes into `main`, create the matching `vX.Y.Z` tag at that commit, and publish a GitHub Release for the tag. Publishing the release triggers PyPI automatically; creating a tag alone does not. The initial stable release is created explicitly, so merging the initial preparation does not publish a package. Subsequent pushes to `main` can create releases automatically through the shared workflow.

For a new repository, configure:

- Access for the repository to call the pinned `NeuralTrust/workflows` workflows.
- `OPENAI_API_KEY` and `GH_TOKEN` for auto-release, with optional `SLACK_WEBHOOK_URL`. `GH_TOKEN` must be a PAT permitted to write release commits through the branch rules; the release event created with it triggers the publishing workflow.
- The `pypi` GitHub environment and a PyPI trusted publisher for owner `NeuralTrust`, repository `neuraltrust-haystack`, workflow `release.yml`, environment `pypi`. No PyPI upload token is used.
- `DEV_GCP_PROJECT_ID` as a repository/organization variable; `DEV_WIF_PROVIDER` and `DEV_WIF_SERVICE_ACCOUNT` as secrets. The workload-identity provider must trust this repository's `develop` ref, and the service account needs write access to `europe-west1/nt-python` in the configured project.
- `DEV_PUBLISH_ENABLED=true` enables publication to the private development registry. GitHub Releases publish to PyPI without an additional enable variable.

Use these dispatches to verify the hosted workflows before publication:

```bash
gh workflow run auto-release.yml --ref main -f dry_run=true
gh workflow run release.yml --ref main
gh workflow run publish-dev.yml --ref develop -f publish=false -f verify_auth=true
```

Run the development check with `verify_auth=false` while registry identity is unconfigured. That checks the package without claiming registry authentication was verified. The authentication check uses the [Artifact Registry permissions API](https://docs.cloud.google.com/artifact-registry/docs/reference/rest/v1/projects.locations.repositories/testIamPermissions); it checks upload permission without uploading a package. Inspect the shared auto-release logs for classification warnings: its fallback to a patch bump does not prove the OpenAI classifier succeeded.

No credentials belong in workflow files. A successful release preflight proves the tested artifact build, not PyPI trusted-publisher registration or an upload. A published GitHub Release triggers PyPI upload; manual Release dispatches only verify artifacts.
