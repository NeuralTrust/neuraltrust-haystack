"""Set the single Hatch/runtime version and validate built release artifacts."""

from __future__ import annotations

import argparse
import re
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = "haystack_integrations/components/guardrails/neuraltrust"
VERSION_PATH = Path("src") / PACKAGE_PATH / "_version.py"
STABLE_VERSION = r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
BUILD_VERSION = rf"{STABLE_VERSION}(?:\.dev[1-9]\d*\+run\.[1-9]\d*)?"
ASSIGNMENT = re.compile(r'^__version__ = "([^"\n]+)"$', re.MULTILINE)


def read_version(source: str) -> str:
    """Read a literal version without importing the package or its dependencies."""
    matches = ASSIGNMENT.findall(source)
    if len(matches) != 1 or not re.fullmatch(BUILD_VERSION, matches[0]):
        raise ValueError("Expected one valid __version__ assignment")
    return matches[0]


def set_version(root: Path, version: str) -> None:
    """Validate before replacing only the version assignment."""
    if not re.fullmatch(BUILD_VERSION, version):
        raise ValueError("Expected X.Y.Z or X.Y.Z.devN+run.N")
    path = root / VERSION_PATH
    source = path.read_text(encoding="utf-8")
    read_version(source)
    path.write_text(ASSIGNMENT.sub(f'__version__ = "{version}"', source), encoding="utf-8")


def development_version(base: str, run_number: int, run_attempt: int) -> str:
    """Create ordered, unique development builds for the next patch version."""
    if not re.fullmatch(STABLE_VERSION, base) or run_number < 1 or run_attempt < 1:
        raise ValueError("Development builds require a stable base and positive run numbers")
    major, minor, patch = map(int, base.split("."))
    return f"{major}.{minor}.{patch + 1}.dev{run_number}+run.{run_attempt}"


def check_artifacts(dist: Path, expected: str) -> None:
    """Check wheel/sdist metadata and their embedded runtime versions."""
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("Expected exactly one wheel and one source distribution")
    with zipfile.ZipFile(wheels[0]) as wheel:
        metadata_files = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise ValueError("Expected one wheel METADATA file")
        check_metadata(wheel.read(metadata_files[0]), expected)
        if read_version(wheel.read(f"{PACKAGE_PATH}/_version.py").decode()) != expected:
            raise ValueError("Wheel runtime version does not match the source version")
    with tarfile.open(sdists[0]) as sdist:
        # Read members without extracting an archive onto the filesystem.
        metadata_files = [
            member for member in sdist.getmembers() if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")
        ]
        version_files = [member for member in sdist.getmembers() if member.name.endswith(f"/{VERSION_PATH}")]
        if len(metadata_files) != 1 or len(version_files) != 1:
            raise ValueError("Expected one source METADATA and runtime version file")
        metadata = sdist.extractfile(metadata_files[0])
        version_file = sdist.extractfile(version_files[0])
        if metadata is None or version_file is None:
            raise ValueError("Source distribution metadata must be regular files")
        check_metadata(metadata.read(), expected)
        if read_version(version_file.read().decode()) != expected:
            raise ValueError("Source distribution runtime version does not match the source version")


def check_metadata(data: bytes, expected: str) -> None:
    """Refuse to publish an artifact with the wrong project or version."""
    metadata = BytesParser().parsebytes(data)
    if metadata["Name"] != "neuraltrust-haystack" or metadata["Version"] != expected:
        raise ValueError("Distribution name or version does not match the project")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setter = commands.add_parser("set")
    setter.add_argument("version")
    dev = commands.add_parser("dev")
    dev.add_argument("--run-number", type=int, required=True)
    dev.add_argument("--run-attempt", type=int, required=True)
    check = commands.add_parser("check")
    check.add_argument("--tag")
    check.add_argument("--dist", type=Path)
    args = parser.parse_args()
    try:
        version = read_version((ROOT / VERSION_PATH).read_text(encoding="utf-8"))
        if args.command == "set":
            # The shared auto-release workflow supplies a bare semantic version.
            if not re.fullmatch(STABLE_VERSION, args.version):
                raise ValueError("Release versions must be X.Y.Z")
            set_version(ROOT, args.version)
        elif args.command == "dev":
            set_version(ROOT, development_version(version, args.run_number, args.run_attempt))
        else:
            if args.tag and (not re.fullmatch(f"v{STABLE_VERSION}", args.tag) or args.tag != f"v{version}"):
                raise ValueError("Release tag must match the stable source version")
            if args.dist:
                check_artifacts(args.dist, version)
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
