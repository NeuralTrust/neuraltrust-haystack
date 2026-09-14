"""Keep ordinary validation offline; production evaluation requires --live."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ["HAYSTACK_TELEMETRY_ENABLED"] = "false"
os.environ["HAYSTACK_CONTENT_TRACING_ENABLED"] = "false"


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="Enable real TrustGuard evaluations")
    parser.addoption("--trustguard-credentials", help="Path to an existing collector credentials JSON file")
    parser.addoption("--live-report", help="Write sanitized production verification evidence here")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        for item in items:
            if "live" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="Production verification requires --live"))


@pytest.fixture(scope="session")
def live_collectors(request):
    filename = request.config.getoption("--trustguard-credentials")
    if not filename:
        pytest.fail("--live requires --trustguard-credentials pointing to existing test collectors")
    try:
        data = json.loads(Path(filename).read_text())
        collectors = data["collectors"]
        assert all(collectors[name]["api_key"] for name in ("allow", "block", "report", "transform"))
    except (OSError, ValueError, KeyError, TypeError, AssertionError):
        pytest.fail("Test collector credentials are missing or malformed", pytrace=False)
    return collectors


@pytest.fixture(scope="session")
def live_evidence(request):
    entries = []
    yield entries
    filename = request.config.getoption("--live-report")
    if filename:
        from importlib.metadata import version

        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                    "haystack_version": version("haystack-ai"),
                    "package_version": version("neuraltrust-haystack"),
                    "endpoint": "https://trustguard.neuraltrust.ai/v1/evaluate",
                    "results": entries,
                },
                indent=2,
            )
            + "\n"
        )
