"""Read-only check of the release token's push permission and effective branch rules."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

# The release creates a normal commit on an existing branch. These rules do not
# prohibit that operation. Other rules require bypass or further verification.
NON_BLOCKING_RULES = {
    "creation",
    "deletion",
    "non_fast_forward",
    "required_linear_history",
    "repository_visibility",
    "repository_delete",
    "repository_transfer",
}


class PermissionCheckError(RuntimeError):
    """The release token is blocked, or its effective permissions are unverified."""


def query(path: str, *, paginate: bool = False):
    """Use GH_TOKEN through gh's environment, never through command arguments."""
    command = ["gh", "api", "--method", "GET", path]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    try:
        # Fixed executable and GET verb, argument list, no shell or credential arguments.
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PermissionCheckError("UNVERIFIED: the read-only GitHub API query could not complete") from exc
    if result.returncode:
        status = re.search(r"HTTP (\d{3})", result.stderr)
        detail = f" (HTTP {status[1]})" if status else ""
        # Never echo raw API errors, stdout, or environment contents.
        raise PermissionCheckError(f"UNVERIFIED: the release token cannot read required GitHub metadata{detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PermissionCheckError("UNVERIFIED: GitHub returned invalid permission metadata") from exc


def verify(repository: str, branch: str) -> str:
    """Check actual ref matches through GitHub instead of approximating globs locally."""
    principal = query("user").get("login")
    if not isinstance(principal, str) or not re.fullmatch(r"[A-Za-z0-9-]+", principal):
        raise PermissionCheckError("UNVERIFIED: GH_TOKEN must identify the release PAT's principal")
    base = f"repos/{repository}"
    push = query(base).get("permissions", {}).get("push")
    if push is not True:
        state = "BLOCKED" if push is False else "UNVERIFIED"
        raise PermissionCheckError(
            f"{state}: GH_TOKEN principal {principal} lacks confirmed repository push permission"
        )
    encoded_branch = quote(branch, safe="")
    query(f"{base}/branches/{encoded_branch}")  # The release updates an existing branch.
    # This endpoint returns only active rules matching this branch, including
    # inherited organization rules; disabled/evaluate rules and other refs vanish.
    pages = query(f"{base}/rules/branches/{encoded_branch}?per_page=100", paginate=True)
    rulesets: dict[int, set[str]] = {}
    for page in pages:
        for rule in page:
            rulesets.setdefault(rule["ruleset_id"], set()).add(rule["type"])
    failures = []
    for ruleset_id, types in sorted(rulesets.items()):
        # The repository endpoint exposes bypass for THIS token, even for an
        # inherited organization ruleset. Repo admin status alone is insufficient.
        ruleset = query(f"{base}/rulesets/{ruleset_id}")
        if ruleset.get("enforcement") != "active":
            raise PermissionCheckError("UNVERIFIED: branch rules changed during the check; rerun the preflight")
        bypass = ruleset.get("current_user_can_bypass")
        restrictions = types - NON_BLOCKING_RULES
        if restrictions and bypass != "always":
            state = "BLOCKED" if restrictions & {"pull_request", "update"} and bypass is not None else "UNVERIFIED"
            failures.append(
                f"ruleset {ruleset_id}: {', '.join(sorted(restrictions))}; token bypass={bypass or 'unknown'}"
            )
            print(f"{state}: {failures[-1]}")
    if failures:
        raise PermissionCheckError(
            f"Release token {principal} cannot be confirmed for a direct push to {branch}: " + "; ".join(failures)
        )
    return (
        f"GH_TOKEN principal {principal}: API-reported push permission and {len(rulesets)} effective ruleset checks "
        f"passed for {branch}. No write was attempted; token write scopes and a real release push remain untested."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository")
    parser.add_argument("branch")
    args = parser.parse_args()
    try:
        if not os.environ.get("GH_TOKEN"):
            raise PermissionCheckError("UNVERIFIED: GH_TOKEN is not configured")
        message = verify(args.repository, args.branch)
    except (PermissionCheckError, KeyError, TypeError, AttributeError) as exc:
        message = (
            str(exc) if isinstance(exc, PermissionCheckError) else "UNVERIFIED: incomplete GitHub permission metadata"
        )
        print(f"::error::{message}")
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(summary).open("a", encoding="utf-8") as stream:
                stream.write(f"## Release permission preflight did not pass\n\n{message}\n")
        raise SystemExit(1) from None
    print(message)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(f"## Release permission preflight passed\n\n{message}\n")


if __name__ == "__main__":
    main()
