"""Release versions must agree before any registry upload can run."""

import io
import runpy
import tarfile
import zipfile
from pathlib import Path

import pytest

release = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "release_version.py"))


@pytest.mark.parametrize("version", ["1.2.3", "0.1.1.dev42+run.2"])
def test_set_version_preserves_surrounding_source(tmp_path, version):
    path = tmp_path / release["VERSION_PATH"]
    path.parent.mkdir(parents=True)
    path.write_text('"""Package version."""\n\n__version__ = "0.1.0"\n')
    release["set_version"](tmp_path, version)
    assert path.read_text() == f'"""Package version."""\n\n__version__ = "{version}"\n'


@pytest.mark.parametrize("version", ["v1.2.3", "01.2.3", "1.2", "1.2.3\n", '1.2.3"; print("bad")'])
def test_invalid_version_does_not_change_source(tmp_path, version):
    with pytest.raises(ValueError):
        release["set_version"](tmp_path, version)
    assert not (tmp_path / release["VERSION_PATH"]).exists()


@pytest.mark.parametrize("source", ["", '__version__ = "garbage"', '__version__ = "0.1.0"\n__version__ = "0.2.0"'])
def test_refuse_ambiguous_version_source(source):
    with pytest.raises(ValueError):
        release["read_version"](source)


def test_development_versions_are_distinct_for_pushes_and_reruns():
    assert release["development_version"]("0.1.0", 42, 1) == "0.1.1.dev42+run.1"
    assert release["development_version"]("0.1.0", 42, 2) == "0.1.1.dev42+run.2"
    assert release["development_version"]("0.1.0", 43, 1) == "0.1.1.dev43+run.1"


@pytest.mark.parametrize("base,run,attempt", [("0.1.0.dev1+run.1", 1, 1), ("0.1.0", 0, 1), ("0.1.0", 1, 0)])
def test_invalid_development_run(base, run, attempt):
    with pytest.raises(ValueError):
        release["development_version"](base, run, attempt)


def make_artifacts(path, wheel_version="0.1.0", sdist_version="0.1.0", runtime_version="0.1.0"):
    runtime = f'__version__ = "{runtime_version}"\n'.encode()
    with zipfile.ZipFile(path / "package.whl", "w") as wheel:
        wheel.writestr("package.dist-info/METADATA", f"Name: neuraltrust-haystack\nVersion: {wheel_version}\n")
        wheel.writestr(f"{release['PACKAGE_PATH']}/_version.py", runtime)
    with tarfile.open(path / "package.tar.gz", "w:gz") as sdist:
        for name, data in {
            "package/PKG-INFO": f"Name: neuraltrust-haystack\nVersion: {sdist_version}\n".encode(),
            f"package/{release['VERSION_PATH']}": runtime,
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            sdist.addfile(member, io.BytesIO(data))


def test_matching_artifact_versions_pass(tmp_path):
    make_artifacts(tmp_path)
    release["check_artifacts"](tmp_path, "0.1.0")


@pytest.mark.parametrize("field", ["wheel_version", "sdist_version", "runtime_version"])
def test_mismatched_artifact_version_prevents_publication(tmp_path, field):
    make_artifacts(tmp_path, **{field: "0.2.0"})
    with pytest.raises(ValueError, match="version"):
        release["check_artifacts"](tmp_path, "0.1.0")


def test_missing_distributions_prevent_publication(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        release["check_artifacts"](tmp_path, "0.1.0")


def test_wrong_project_name_prevents_publication():
    with pytest.raises(ValueError, match="name or version"):
        release["check_metadata"](b"Name: another-package\nVersion: 0.1.0\n", "0.1.0")


def test_stale_extra_distribution_prevents_publication(tmp_path):
    make_artifacts(tmp_path)
    (tmp_path / "stale.whl").touch()
    with pytest.raises(ValueError, match="exactly one"):
        release["check_artifacts"](tmp_path, "0.1.0")


@pytest.mark.parametrize("tag", ["v0.2.0", "0.1.0", "v0.1.0.dev1+run.1"])
def test_release_tag_mismatch_prevents_publication(monkeypatch, tmp_path, tag):
    path = tmp_path / release["VERSION_PATH"]
    path.parent.mkdir(parents=True)
    path.write_text('__version__ = "0.1.0"\n')
    monkeypatch.setitem(release["main"].__globals__, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["release_version.py", "check", "--tag", tag])
    with pytest.raises(SystemExit) as error:
        release["main"]()
    assert error.value.code == 2
