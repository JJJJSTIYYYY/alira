"""Deployment fingerprints include every local requirements/constraints input."""

import shutil
import socket

import pytest

from alira.kernel.module.dependencies import DependencyError
from alira.kernel.module.dependencies.requirements import requirements_fingerprint


@pytest.mark.parametrize(
    "option",
    [
        "-r child.txt",
        "--requirement child.txt",
        "-rchild.txt",
        "--requirement=child.txt",
        "-c child.txt",
        "--constraint child.txt",
        "-cchild.txt",
        "--constraint=child.txt",
    ],
)
def test_included_inputs_change_fingerprint(tmp_path, option):
    root = tmp_path / "requirements.txt"
    root.write_text(option + "\n")
    child = tmp_path / "child.txt"
    child.write_text("package>=1\n")
    original = requirements_fingerprint(root)
    assert requirements_fingerprint(root) == original
    child.write_text("package>=2\n")
    assert requirements_fingerprint(root) != original


def test_nested_paths_are_relative_to_the_including_file(tmp_path, monkeypatch):
    root = tmp_path / "requirements.txt"
    child = tmp_path / "nested/child.txt"
    child.parent.mkdir()
    child.write_text("-c ../constraints.txt\n")
    constraint = tmp_path / "constraints.txt"
    constraint.write_text("package==1\n")
    root.write_text("-r nested/child.txt\n")
    initial = requirements_fingerprint(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert requirements_fingerprint(root) == initial
    constraint.write_text("package==2\n")
    assert requirements_fingerprint(root) != initial


def test_quoted_paths_continuations_and_comments(tmp_path):
    root = tmp_path / "requirements.txt"
    child = tmp_path / "child # requirements.txt"
    child.write_text("package[extra]>=1; python_version >= '3.12'\n")
    root.write_text(
        '# ignored -r missing.txt\n--requirement \\\n  "child # requirements.txt" # -c missing.txt\n'
    )
    initial = requirements_fingerprint(root)
    child.write_text("package>=2\n")
    assert requirements_fingerprint(root) != initial


def test_cycle_and_duplicate_references_are_finite_and_deterministic(tmp_path):
    root = tmp_path / "requirements.txt"
    child = tmp_path / "child.txt"
    root.write_text("-r child.txt\n-r child.txt\n")
    child.write_text("-c requirements.txt\npackage>=1\n")
    initial = requirements_fingerprint(root)
    assert requirements_fingerprint(root) == initial
    child.write_text("-c requirements.txt\npackage>=2\n")
    assert requirements_fingerprint(root) != initial


def test_equivalent_directory_trees_have_the_same_fingerprint(tmp_path):
    left = tmp_path / "left"
    left.mkdir()
    (left / "requirements.txt").write_text("-r child.txt\n")
    (left / "child.txt").write_text("package>=1\n")
    right = tmp_path / "right"
    shutil.copytree(left, right)
    assert requirements_fingerprint(
        left / "requirements.txt"
    ) == requirements_fingerprint(right / "requirements.txt")


def test_local_file_uri_is_supported(tmp_path):
    root = tmp_path / "requirements.txt"
    child = tmp_path / "child with spaces.txt"
    child.write_text("package>=1\n")
    root.write_text(f"-r {child.as_uri()}\n")
    initial = requirements_fingerprint(root)
    child.write_text("package>=2\n")
    assert requirements_fingerprint(root) != initial


def test_missing_included_file_is_actionable(tmp_path):
    root = tmp_path / "requirements.txt"
    root.write_text("-r missing.txt\n")
    with pytest.raises(
        DependencyError, match="Cannot read requirements input.*missing.txt"
    ):
        requirements_fingerprint(root)


@pytest.mark.parametrize("content", ["-r\n", "--constraint=\n", "-r 'unterminated\n"])
def test_invalid_inputs_have_source_locations(tmp_path, content):
    root = tmp_path / "requirements.txt"
    root.write_text(content)
    with pytest.raises(DependencyError, match="Invalid requirements include"):
        requirements_fingerprint(root)


def test_remote_urls_are_stable_inputs_without_network_access(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Fingerprinting must not use the network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    root = tmp_path / "requirements.txt"
    root.write_text(
        "-r https://example.com/requirements.txt\n-c http://example.com/constraints.txt\n"
    )
    initial = requirements_fingerprint(root)
    assert requirements_fingerprint(root) == initial
    root.write_text("-r https://example.com/updated.txt\n")
    assert requirements_fingerprint(root) != initial


def test_missing_requirements_need_no_fingerprint():
    assert requirements_fingerprint(None) is None
