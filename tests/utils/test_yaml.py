"""YAML helpers retain values, perform their documented updates and expose errors."""

import importlib
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from alira.utils.yaml import (
    append_to_yaml,
    load_from_yaml,
    update_to_yaml,
    write_to_yaml,
)

yaml_utils = importlib.import_module("alira.utils.yaml")


@pytest.mark.parametrize("path_type", [str, Path], ids=["str", "Path"])
def test_write_creates_parent_directories_and_round_trips_unicode(tmp_path, path_type):
    path = tmp_path / "nested" / "settings.yaml"
    data = {"name": "上海", "enabled": False, "count": 0, "items": ["001", None]}

    write_to_yaml(path_type(path), data)

    assert load_from_yaml(path_type(path)) == data
    assert "上海" in path.read_text(encoding="utf-8")


def test_write_overwrites_old_keys(tmp_path):
    path = tmp_path / "settings.yaml"
    write_to_yaml(path, {"old": 1, "retained": 2})
    write_to_yaml(path, {"new": 3})
    assert load_from_yaml(path) == {"new": 3}


def test_missing_file_and_missing_key(tmp_path):
    path = tmp_path / "missing.yaml"
    assert load_from_yaml(path) == {}
    assert load_from_yaml(path, "missing") is None
    assert not path.exists()


def test_key_selection_preserves_falsy_keys_and_values(tmp_path):
    path = tmp_path / "settings.yaml"
    data = {"": "empty-key", "zero": 0, "flag": False, "null": None, "nested": {"x": 1}}
    write_to_yaml(path, data)
    for key, value in data.items():
        assert load_from_yaml(path, key) == value
    assert load_from_yaml(path, "missing") is None


@pytest.mark.parametrize("initial", [None, "", "# only a comment\n"])
def test_append_creates_or_populates_an_empty_file(tmp_path, initial):
    path = tmp_path / "settings.yaml"
    if initial is not None:
        path.write_text(initial)
    append_to_yaml(path, {"name": "上海"})
    assert load_from_yaml(path) == {"name": "上海"}


def test_append_merges_only_top_level_keys_and_preserves_input(tmp_path):
    path = tmp_path / "settings.yaml"
    write_to_yaml(path, {"kept": 1, "nested": {"old": 2}, "items": [1]})
    new_data = {"nested": {"new": 3}, "items": [2], "added": False}

    append_to_yaml(path, new_data)

    assert load_from_yaml(path) == {
        "kept": 1, "nested": {"new": 3}, "items": [2], "added": False
    }
    assert new_data == {"nested": {"new": 3}, "items": [2], "added": False}


@pytest.mark.parametrize("initial", [None, "", "# only a comment\n"])
def test_update_creates_or_populates_an_empty_file(tmp_path, initial):
    path = tmp_path / "settings.yaml"
    if initial is not None:
        path.write_text(initial)
    assert update_to_yaml(path, "title", "中文") == {"title": "中文"}
    assert load_from_yaml(path) == {"title": "中文"}


def test_update_preserves_content_whitespace_and_unrelated_keys(tmp_path):
    path = tmp_path / "settings.yaml"
    write_to_yaml(path, {"title": "old", "kept": "value"})
    expected = {"title": "  new content\n", "kept": "value"}
    assert update_to_yaml(path, "title", "  new content\n") == expected
    assert load_from_yaml(path) == expected


@pytest.mark.parametrize("content", ["", " \t\n"])
@pytest.mark.parametrize("title", ["title", "missing"])
def test_empty_update_deletes_only_the_selected_key(tmp_path, content, title):
    path = tmp_path / "settings.yaml"
    write_to_yaml(path, {"title": "old", "kept": "value"})
    expected = {"kept": "value"} if title == "title" else {"title": "old", "kept": "value"}
    assert update_to_yaml(path, title, content) == expected
    assert load_from_yaml(path) == expected


@pytest.mark.parametrize(
    "operation,prefix",
    [
        (lambda path: load_from_yaml(path), "Error loading yaml file:"),
        (lambda path: append_to_yaml(path, {"new": 1}), "Error appending to yaml file:"),
        (lambda path: update_to_yaml(path, "new", "value"), "Error updating to yaml file:"),
    ],
    ids=["load", "append", "update"],
)
def test_malformed_yaml_is_reported_and_never_overwritten(tmp_path, monkeypatch, operation, prefix):
    path = tmp_path / "broken.yaml"
    original = "items: [unterminated"
    path.write_text(original)
    report = Mock()
    monkeypatch.setattr(yaml_utils.logger, "error", report)

    with pytest.raises(yaml.YAMLError):
        operation(path)

    report.assert_called_once()
    assert report.call_args.args[0].startswith(prefix)
    assert path.read_text() == original


@pytest.mark.parametrize(
    "operation,prefix",
    [
        (lambda path: load_from_yaml(path), "Error loading yaml file:"),
        (lambda path: write_to_yaml(path, {"new": 1}), "Error writing to yaml file:"),
        (lambda path: append_to_yaml(path, {"new": 1}), "Error appending to yaml file:"),
        (lambda path: update_to_yaml(path, "new", "value"), "Error updating to yaml file:"),
    ],
    ids=["load", "write", "append", "update"],
)
def test_file_errors_are_logged_and_propagated(tmp_path, monkeypatch, operation, prefix):
    report = Mock()
    monkeypatch.setattr(yaml_utils.logger, "error", report)

    with pytest.raises(IsADirectoryError):
        operation(tmp_path)

    report.assert_called_once()
    assert report.call_args.args[0].startswith(prefix)
