"""A green release preflight must not hide a protected branch or unreadable rules."""

import json
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

release = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "check_release_permissions.py"))
PermissionCheckError = release["PermissionCheckError"]


def fake_api(monkeypatch, *, bypass="never", push=True, types=("pull_request",), enforcement="active"):
    calls = []

    def query(path, *, paginate=False):
        calls.append((path, paginate))
        if path == "user":
            return {"login": "release-bot"}
        if path == "repos/NeuralTrust/neuraltrust-haystack":
            return {"permissions": {"push": push}}
        if "/branches/" in path and "/rules/" not in path:
            return {"name": "main"}
        if "/rules/branches/" in path:
            return [[{"ruleset_id": 9256258, "type": kind} for kind in types], []]
        if path.endswith("/rulesets/9256258"):
            return {"enforcement": enforcement, "current_user_can_bypass": bypass}
        raise AssertionError(path)

    monkeypatch.setitem(release["verify"].__globals__, "query", query)
    return calls


@pytest.mark.parametrize("bypass", ["never", "pull_requests_only", None])
def test_pull_request_rule_requires_unconditional_bypass(monkeypatch, bypass):
    fake_api(monkeypatch, bypass=bypass)
    with pytest.raises(PermissionCheckError, match="ruleset 9256258: pull_request"):
        release["verify"]("NeuralTrust/neuraltrust-haystack", "main")


@pytest.mark.parametrize("bypass", ["always", "exempt"])
def test_release_pat_with_effective_bypass_passes(monkeypatch, bypass):
    calls = fake_api(monkeypatch, bypass=bypass)
    result = release["verify"]("NeuralTrust/neuraltrust-haystack", "main")
    assert "release-bot" in result
    assert "No write was attempted" in result
    assert ("repos/NeuralTrust/neuraltrust-haystack/rules/branches/main?per_page=100", True) in calls
    assert ("repos/NeuralTrust/neuraltrust-haystack/rulesets/9256258", False) in calls


@pytest.mark.parametrize("push", [False, None])
def test_repo_admin_inference_cannot_replace_push_permission(monkeypatch, push):
    calls = fake_api(monkeypatch, bypass="always", push=push)
    with pytest.raises(PermissionCheckError, match="lacks confirmed repository push permission"):
        release["verify"]("NeuralTrust/neuraltrust-haystack", "main")
    assert not any("rulesets" in path for path, _ in calls)


def test_normal_commit_does_not_require_deletion_or_force_push_bypass(monkeypatch):
    fake_api(monkeypatch, types=("deletion", "non_fast_forward", "required_linear_history"))
    assert "passed for main" in release["verify"]("NeuralTrust/neuraltrust-haystack", "main")


@pytest.mark.parametrize("rule", ["update", "required_status_checks", "required_signatures", "unknown_future_rule"])
def test_other_restrictive_rules_cannot_silently_pass(monkeypatch, rule):
    fake_api(monkeypatch, types=(rule,))
    with pytest.raises(PermissionCheckError, match=rule):
        release["verify"]("NeuralTrust/neuraltrust-haystack", "main")


def test_effective_api_handles_ref_matching_and_pagination(monkeypatch):
    calls = fake_api(monkeypatch, types=())
    assert "0 effective ruleset checks" in release["verify"]("NeuralTrust/neuraltrust-haystack", "release/main")
    assert ("repos/NeuralTrust/neuraltrust-haystack/rules/branches/release%2Fmain?per_page=100", True) in calls
    assert not any("rulesets" in path for path, _ in calls)


def test_changed_enforcement_is_unverified(monkeypatch):
    fake_api(monkeypatch, bypass="always", enforcement="evaluate")
    with pytest.raises(PermissionCheckError, match="UNVERIFIED: branch rules changed"):
        release["verify"]("NeuralTrust/neuraltrust-haystack", "main")


@pytest.mark.parametrize("status", [401, 403, 404])
def test_unreadable_metadata_fails_without_echoing_sensitive_api_output(monkeypatch, status):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="secret-value", stderr=f"secret-value (HTTP {status})"
        ),
    )
    with pytest.raises(PermissionCheckError, match=f"UNVERIFIED:.*HTTP {status}") as error:
        release["query"]("user")
    assert "secret-value" not in str(error.value)


def test_cli_queries_are_get_only_and_do_not_include_credentials(monkeypatch):
    def run(command, **kwargs):
        assert command == ["gh", "api", "--method", "GET", "rules", "--paginate", "--slurp"]
        assert kwargs["capture_output"] is True
        return SimpleNamespace(returncode=0, stdout=json.dumps([[], []]))

    monkeypatch.setattr(subprocess, "run", run)
    assert release["query"]("rules", paginate=True) == [[], []]


def test_missing_secret_fails_before_api_call(monkeypatch, tmp_path):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr("sys.argv", ["check_release_permissions.py", "NeuralTrust/neuraltrust-haystack", "main"])
    with pytest.raises(SystemExit) as error:
        release["main"]()
    assert error.value.code == 1
    assert "UNVERIFIED: GH_TOKEN is not configured" in (tmp_path / "summary.md").read_text()
