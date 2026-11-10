"""YAML lookup preserves explicit values and rejects malformed documents."""

import pytest
import yaml

from alira.utils.config import YamlConfig


def test_config_preserves_values_and_defaults_for_missing_or_null(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "nested": {"false": False, "zero": 0, "empty": "", "null": None},
    }))
    config = YamlConfig(path)
    assert config.get("nested.false", True) is False
    assert config.get("nested.zero", 42) == 0
    assert config.get("nested.empty", "default") == ""
    assert config.get("nested.null", "default") == "default"
    assert config.get("missing.key", "default") == "default"
    assert config.get("nested.zero.key", "default") == "default"


@pytest.mark.parametrize("content", [None, "", "null\n"])
def test_missing_or_empty_configuration_uses_defaults(tmp_path, content):
    path = tmp_path / "config.yaml"
    if content is not None:
        path.write_text(content)
    assert YamlConfig(path).get("missing", 42) == 42


@pytest.mark.parametrize("content", ["[]", "42", "false", "plain text"])
def test_configuration_requires_a_mapping(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    with pytest.raises(ValueError, match="YAML mapping"):
        YamlConfig(path)


def test_invalid_yaml_is_reported(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("invalid: [")
    with pytest.raises(yaml.YAMLError):
        YamlConfig(path)
