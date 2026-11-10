"""Each module reads only its adjacent YAML file, regardless of cwd."""

import os
from pathlib import Path
import runpy

import pytest
import yaml

from alira.shared.application import config as application_config

MODULES_DIR = Path(__file__).resolve().parents[2] / "src/alira/modules"


def load_module_settings(tmp_path, monkeypatch, name, values=None):
    module_dir = tmp_path / name
    module_dir.mkdir()
    script = module_dir / "config.py"
    script.write_text((MODULES_DIR / name / "config.py").read_text())
    if values is not None:
        script.with_suffix(".yaml").write_text(yaml.safe_dump(values))
    # The working directory and Apixis namespace must not configure this module.
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "base_url": "http://wrong-host:9999",
        "worker_count": 99,
        "generation_ttl": 99,
        "APIXIS": {"DATABASE": {"worker_count": 99}},
    }))
    monkeypatch.chdir(tmp_path)
    return runpy.run_path(str(script))


@pytest.mark.parametrize("name", ["web", "agent", "database"])
def test_module_defaults_ignore_working_directory_configuration(tmp_path, monkeypatch, name):
    settings = load_module_settings(tmp_path, monkeypatch, name)
    if name == "web":
        assert settings["BASE_URL"] == "http://localhost:2712"
    elif name == "agent":
        assert settings["GENERATION_TTL"] == 600
        assert settings["CONTAINER_TTL"] == 6000
        assert settings["GRAPH_CACHE_TTL"] == 600
    else:
        assert settings["WORKER_COUNT"] == 4
        assert settings["SQLITE_DATABASE"] == os.path.join(
            application_config.ALIRA_BASE_DIR, "sqlite", "alira.sqlite3"
        )
        assert settings["SQLITE_EMBEDDING_MODEL"] == ""
        assert settings["SQLITE_EMBEDDING_ENABLE"] is False
        assert settings["SQLITE_EMBEDDING_OPTIONS"] == {}
        assert settings["VECTOR_DIMENSION"] == 640
        assert settings["HOT_CACHE_DEFAULT_EXPIRE_SECONDS"] == 600
        assert settings["STATIC_CACHE_DEFAULT_EXPIRE_SECONDS"] == 604800


def test_web_configuration_is_module_local(tmp_path, monkeypatch):
    settings = load_module_settings(tmp_path, monkeypatch, "web", {
        "base_url": "http://127.0.0.1:8080",
    })
    assert settings["BASE_URL"] == "http://127.0.0.1:8080"


def test_agent_configuration_is_module_local(tmp_path, monkeypatch):
    settings = load_module_settings(tmp_path, monkeypatch, "agent", {
        "generation_ttl": 17, "container_ttl": 29, "graph_cache_ttl": 41,
    })
    assert settings["GENERATION_TTL"] == 17
    assert settings["CONTAINER_TTL"] == 29
    assert settings["GRAPH_CACHE_TTL"] == 41


def test_database_configuration_is_module_local(tmp_path, monkeypatch):
    settings = load_module_settings(tmp_path, monkeypatch, "database", {
        "worker_count": 2,
        "sqlite": {
            "database": "/tmp/custom-alira.sqlite3",
            "embedding_model": "/models/embedding",
            "embedding_enable": True,
            "embedding_options": {"device": "cpu", "query_prompt_name": "retrieval"},
            "vector_dimension": 768,
        },
        "cache": {"hot_cache_default_expire_seconds": 17, "static_cache_default_expire_seconds": 29},
    })
    assert settings["WORKER_COUNT"] == 2
    assert settings["SQLITE_DATABASE"] == "/tmp/custom-alira.sqlite3"
    assert settings["SQLITE_EMBEDDING_MODEL"] == "/models/embedding"
    assert settings["SQLITE_EMBEDDING_ENABLE"] is True
    assert settings["SQLITE_EMBEDDING_OPTIONS"] == {"device": "cpu", "query_prompt_name": "retrieval"}
    assert settings["VECTOR_DIMENSION"] == 768
    assert settings["HOT_CACHE_DEFAULT_EXPIRE_SECONDS"] == 17
    assert settings["STATIC_CACHE_DEFAULT_EXPIRE_SECONDS"] == 29
