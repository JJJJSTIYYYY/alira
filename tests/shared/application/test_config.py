"""Application settings stay independent from module and Apixis settings."""

import runpy

import pytest
import yaml

from alira.shared.application import config


@pytest.mark.parametrize("base_dir", [None, "/tmp/alira-data"])
def test_application_configuration_reads_its_own_namespace(monkeypatch, tmp_path, base_dir):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "SERVER": {"base_dir": base_dir},
        "LOG": {"debug_level": "WARN", "trace": False, "log_buffer_size": 32},
        "RUNTIME": {"cache_clean_interval": 41},
        "APIXIS": {"LOG": {"debug_level": "ERROR"}},
        "DATABASE": {"worker_count": 99},
    }))
    monkeypatch.chdir(tmp_path)
    settings = runpy.run_path(config.__file__)
    assert settings["ALIRA_BASE_DIR"] == (base_dir or "./.alira_data/")
    assert settings["DEBUG_LEVEL"] == "WARN"
    assert settings["TRACE"] is False
    assert settings["LOG_BUFFER_SIZE"] == 32
    assert settings["MAX_LOG_FILE_SIZE"] == 5242880
    assert settings["CACHE_CLEAN_INTERVAL"] == 41
    assert {
        "BASE_URL", "WORKER_COUNT", "SQLITE_DATABASE", "SQLITE_EMBEDDING_OPTIONS",
        "HOT_CACHE_DEFAULT_EXPIRE_SECONDS", "GENERATION_TTL", "CONTAINER_TTL",
        "GRAPH_CACHE_TTL",
    }.isdisjoint(settings)


def test_application_defaults_without_configuration(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = runpy.run_path(config.__file__)
    assert settings["ALIRA_BASE_DIR"] == "./.alira_data/"
    assert settings["DEBUG_LEVEL"] == "DEBUG"
    assert settings["TRACE"] is True
    assert settings["CACHE_CLEAN_INTERVAL"] == 300
