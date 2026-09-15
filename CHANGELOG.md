# Changelog

## [Unreleased]

## [v0.1.0] — 2026-09-15

### Added

- `NeuralTrustGuard` and `NeuralTrustChatGuard` for synchronous and asynchronous evaluation of text and text-only chat in input and output policy phases.
- Handling for `allow`, `report`, `transform`, `block`, and `ask` verdicts, with strict transformation validation and fail-closed errors. Route mode prevents blocked content from reaching required downstream inputs.
- Haystack Secret configuration, pipeline serialization, request context, and bounded retries for transient failures.
- Reusable HTTP connection pools, explicit synchronous/asynchronous cleanup, and asynchronous client/TLS setup that does not block the event loop.
- Support for Python 3.10+ and Haystack 2.31/3.x, with offline component, pipeline, compatibility, packaging, and opt-in production tests.
- [Official Haystack integration documentation](https://docs.neuraltrust.ai/integrations/haystack), runnable text/chat examples, and contributor guidance.
- Shared NeuralTrust CI and automatic release versioning, trusted PyPI publishing, and unique development artifacts. Release checks validate token permissions, tags, source versions, and wheel/source-distribution metadata before publication.

### Changed

- Run shared lint once and test all ten Python/Haystack combinations in isolated environments, with individual results in the job summary. Pull requests no longer receive duplicate CI runs from `develop` pushes.
- Run PR metadata validation separately so title and description edits do not repeat the full test matrix.
- Use Node24 artifact actions and standard package installation instructions, with documentation and changelog links in package metadata.

### Fixed

- Prevent PR metadata edits from cancelling active code checks and eliminate malformed names from unused shared lint/test jobs.
- Recognize GitHub's `exempt` release-token permission mode when validating access through inherited branch rules.
