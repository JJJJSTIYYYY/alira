"""Tests for Alira-specific configuration and shared configuration ownership."""

import os
import runpy

import pytest
from apixis.core.config import base as shared_config

from alira.config import base_config


def test_business_settings_use_apixis_configuration():
    assert base_config._get_config is shared_config._get_config
    assert base_config.ALIRA_BASE_DIR == shared_config._get_config(
        "SERVER.base_dir", "./.alira_data/"
    )
    assert base_config.SQLITE_DATABASE == shared_config._get_config(
        "DATABASE.sqlite.database",
        os.path.join(base_config.ALIRA_BASE_DIR, "sqlite", "alira.sqlite3"),
    )


@pytest.mark.parametrize("base_dir", [None, "/tmp/alira-data"])
def test_application_owns_storage_root_configuration(monkeypatch, base_dir):
    monkeypatch.setattr(shared_config, "_config", {"SERVER": {"base_dir": base_dir}})
    settings = runpy.run_path(base_config.__file__)
    assert settings["ALIRA_BASE_DIR"] == (base_dir or "./.alira_data/")
    assert settings["SQLITE_DATABASE"] == os.path.join(
        settings["ALIRA_BASE_DIR"], "sqlite", "alira.sqlite3"
    )


@pytest.mark.parametrize("data_store", ["sqlite", "mysql"])
@pytest.mark.parametrize("cache_store", ["builtin", "redis"])
def test_storage_settings_ignore_removed_backends(monkeypatch, data_store, cache_store):
    monkeypatch.setattr(
        shared_config,
        "_config",
        {
            "REMOTE_GATEWAY": {"enable": True},
            "EVENT_CHANNEL": {"type": "kafka"},
            "DATABASE": {
                "sqlite": {"database": "/tmp/custom-alira.sqlite3"},
                "worker_count": 2,
            },
            "DATA_STORE": {
                "type": data_store,
                "sqlite": {"database": "/tmp/obsolete.sqlite3"},
                "mysql": {
                    "base_url": "unused",
                    "port": 3307,
                    "user": "unused",
                    "password": "unused",
                    "database": "unused",
                    "charset": "utf8mb4",
                    "auto_commit": False,
                },
            },
            "DATACACHE": {
                "hot_cache_default_expire_seconds": 17,
                "static_cache_default_expire_seconds": 29,
            },
            "CACHE": {
                "store_type": cache_store,
                "redis": {"url": "redis://unused", "pool_size": 99},
                "hot_cache_default_expire_seconds": 99,
                "static_cache_default_expire_seconds": 99,
            },
            "RUNTIME": {"cache_clean_interval": 41},
        },
    )
    settings = runpy.run_path(base_config.__file__)
    assert settings["SQLITE_DATABASE"] == "/tmp/custom-alira.sqlite3"
    assert settings["WORKER_COUNT"] == 2
    assert settings["HOT_CACHE_DEFAULT_EXPIRE_SECONDS"] == 17
    assert settings["STATIC_CACHE_DEFAULT_EXPIRE_SECONDS"] == 29
    assert settings["CACHE_CLEAN_INTERVAL"] == 41
    assert {
        "DATA_STORE_TYPE",
        "CACHE_STORE_TYPE",
        "MEMO_REDIS_URL",
        "REDIS_POOL_SIZE",
        "MYSQL_BASE_URL",
        "MYSQL_PORT",
        "MYSQL_USER",
        "MYSQL_PASSWORD",
        "MYSQL_DATABASE",
        "MYSQL_CHARSET",
        "AUTO_COMMIT",
    }.isdisjoint(settings)
