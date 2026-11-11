"""Application settings stay independent from module and Apixis settings."""

from pathlib import Path
import runpy

import pytest
import yaml

from alira.shared.application import config


@pytest.mark.parametrize("custom_base_dir", [False, True])
def test_application_configuration_reads_its_own_namespace(monkeypatch, tmp_path, custom_base_dir):
    base_dir = str(tmp_path / "alira-data") if custom_base_dir else None
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "SERVER": {"base_dir": base_dir},
        "LOG": {"debug_level": "WARN", "trace": False, "log_buffer_size": 32},
        "RUNTIME": {"cache_clean_interval": 41},
        "APIXIS": {"LOG": {"debug_level": "ERROR"}},
        "DATABASE": {"worker_count": 99},
    }))
    monkeypatch.chdir(tmp_path)
    settings = runpy.run_path(config.__file__)
    expected = Path(base_dir).resolve() if base_dir else (Path.home() / ".alira").resolve()
    assert settings["ALIRA_BASE_DIR"] == str(expected)
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
    assert settings["ALIRA_BASE_DIR"] == str((Path.home() / ".alira").resolve())
    assert settings["DEBUG_LEVEL"] == "DEBUG"
    assert settings["TRACE"] is True
    assert settings["CACHE_CLEAN_INTERVAL"] == 300


@pytest.mark.parametrize("base_dir", [None, "./.alira/", "custom-data", "~/custom-data"])
def test_storage_root_is_absolute_and_independent_of_working_directory(
    monkeypatch, tmp_path, base_dir,
):
    home = tmp_path / "用户 home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    roots = []
    for name in ("project", "elsewhere"):
        working_dir = tmp_path / name
        working_dir.mkdir()
        if base_dir is not None:
            (working_dir / "config.yaml").write_text(yaml.safe_dump({
                "SERVER": {"base_dir": base_dir},
            }))
        monkeypatch.chdir(working_dir)
        roots.append(runpy.run_path(config.__file__)["ALIRA_BASE_DIR"])
    assert roots[0] == roots[1]
    assert Path(roots[0]).is_absolute()
    expected = home / (".alira" if base_dir in (None, "./.alira/") else "custom-data")
    assert roots[0] == str(expected.resolve())
