"""In-memory cache, TTL operations, and JSON persistence."""

import asyncio
import copy
import json
import os
import time
from pathlib import Path
from typing import Any

from alira.shared.application.config import (
    ALIRA_BASE_DIR,
    HOT_CACHE_DEFAULT_EXPIRE_SECONDS,
    STATIC_CACHE_DEFAULT_EXPIRE_SECONDS,
)
from alira.modules.database.services.cache.keys import (
    build_conversation_key,
    build_resource_key,
    build_resource_prefix,
)
from alira.modules.database.utils.handlers import service_handler
from alira.modules.database.utils.serialization import decode_message_row
from alira.utils.logger import logger
from alira.utils.serialization import json_default


class CacheService:
    """TTL cache for copied message, branch-chain and resource values.

    All payload methods return ``success`` and ``messages``. Failures return
    ``{"success": False, "messages": "fail: <error>"}``; ``cache_hit`` is
    present only on successful reads. Reads return disposable deep copies by
    default; borrowed message snapshots expose read-only records to the executor.
    """

    def __init__(self, persistence_path: str | Path | None = None):
        self._persistence_path = Path(
            persistence_path
            or (Path(ALIRA_BASE_DIR) / "builtin_cache_store.json")
            .expanduser()
            .resolve()
        )
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        """Restore valid saved entries once, preserving current in-memory values."""
        async with self._lock:
            if self._started:
                return
            self._started = True
            if not self._persistence_path.exists():
                return

            try:
                with self._persistence_path.open("r", encoding="utf-8") as file:
                    stored_cache = json.load(file)
                if not isinstance(stored_cache, dict):
                    raise TypeError("cache persistence content must be an object")

                now = time.time()
                restored = {}
                for key, item in stored_cache.items():
                    if key in self._cache or not self._is_valid_item(key, item, now):
                        continue
                    if key.startswith("memo:"):
                        for message in item["value"]:
                            decode_message_row(message)
                    restored[key] = item
                self._cache = restored | self._cache
            except (OSError, ValueError, TypeError) as error:
                logger.warning(f"Unable to restore builtin cache: {error}")

    async def stop(self) -> None:
        """Persist valid entries atomically for restoration on the next start."""
        async with self._lock:
            self._purge_expired()
            self._persistence_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = self._persistence_path.with_suffix(
                f"{self._persistence_path.suffix}.tmp"
            )
            try:
                with temporary_path.open("w", encoding="utf-8") as file:
                    json.dump(
                        self._cache,
                        file,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=json_default,
                    )
                os.replace(temporary_path, self._persistence_path)
                self._started = False
            except (OSError, TypeError, ValueError) as error:
                logger.exception(f"Unable to persist builtin cache: {error}")
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

    @service_handler
    async def append_message(
        self, payload: dict, *, take_ownership: bool = False
    ) -> dict:
        """Append to an existing conversation cache and refresh its hot TTL.

        Payload requires user_uid and conversation_uid; message defaults to {}.
        Returns success with messages="success", including on a cache miss.
        take_ownership consumes an exclusively owned, normalized record; its
        caller must not modify that record after handing it to the cache."""
        key = build_conversation_key(payload)
        message = payload.get("message", {})
        if not take_ownership and not isinstance(message, dict):
            raise TypeError("Message must be an object")
        async with self._lock:
            item = self._get_item(key)
            if item is None:
                logger.warning("Builtin cache key not exists.")
                return {"success": True, "messages": "success"}
            if not take_ownership:
                message = decode_message_row(copy.deepcopy(message))
            item["value"].append(message)
            item["expires_at"] = time.time() + HOT_CACHE_DEFAULT_EXPIRE_SECONDS
        return {"success": True, "messages": "success"}

    @service_handler
    async def set_messages(
        self, payload: dict, *, take_ownership: bool = False
    ) -> dict:
        """Replace cached messages and refresh their hot TTL.

        Payload requires user_uid and conversation_uid; messages defaults to []
        and must be a list. An empty list removes the cache entry.
        Returns success with messages="success". take_ownership consumes an
        exclusively owned list of normalized records; the caller subsequently
        treats both that list and its records as read-only."""
        key = build_conversation_key(payload)
        messages = payload.get("messages", [])
        if not take_ownership and (
            not isinstance(messages, list)
            or not all(isinstance(message, dict) for message in messages)
        ):
            raise ValueError("Messages must be a list of objects")
        async with self._lock:
            # Empty message lists are treated as cache misses.
            if messages:
                if not take_ownership:
                    messages = copy.deepcopy(messages)
                    for message in messages:
                        decode_message_row(message)
                self._set_item(
                    key,
                    messages,
                    HOT_CACHE_DEFAULT_EXPIRE_SECONDS,
                )
            else:
                self._cache.pop(key, None)
        return {"success": True, "messages": "success"}

    @service_handler
    async def get_messages(
        self, payload: dict, *, borrow_records: bool = False
    ) -> dict:
        """Read all cached messages for user_uid and conversation_uid.

        Returns success, messages=list and cache_hit=bool. Missing, expired or
        empty message caches return messages=[] and cache_hit=False.
        borrow_records returns a new list containing read-only cache records,
        allowing branch selection before copying the selected records."""
        key = build_conversation_key(payload)
        async with self._lock:
            return self._read_result(key, empty_is_miss=True, deep=not borrow_records)

    @service_handler
    async def set_branch_chain(self, payload: dict) -> dict:
        """Replace a conversation branch chain and refresh its static TTL.

        Payload requires user_uid and conversation_uid; node_id_chain defaults
        to [] and must be a list. Returns success with messages="success"."""
        node_id_chain = payload.get("node_id_chain", [])
        if not isinstance(node_id_chain, list) or not all(
            isinstance(node_id, str) for node_id in node_id_chain
        ):
            raise ValueError("node_id_chain must be a list of strings")
        key = build_conversation_key(payload, prefix="chain")
        async with self._lock:
            self._set_item(
                key,
                node_id_chain.copy(),
                STATIC_CACHE_DEFAULT_EXPIRE_SECONDS,
            )
        return {"success": True, "messages": "success"}

    @service_handler
    async def update_branch_chain(self, payload: dict) -> dict:
        """Replace the cached chain after parent_id with node_id.

        Payload requires user_uid, conversation_uid, node_id and parent_id.
        Returns success with messages="success", "cache not found",
        "parent_id not found in cache" or "already up to date". Only a changed
        chain refreshes its static TTL."""
        node_id = payload["node_id"]
        parent_id = payload["parent_id"]
        if not isinstance(node_id, str) or not isinstance(parent_id, str):
            raise TypeError("node_id and parent_id must be strings")
        key = build_conversation_key(payload, prefix="chain")
        async with self._lock:
            item = self._get_item(key)
            if item is None:
                return {"success": True, "messages": "cache not found"}
            node_id_chain = item["value"]
            try:
                parent_index = node_id_chain.index(parent_id)
            except ValueError:
                return {
                    "success": True,
                    "messages": "parent_id not found in cache",
                }
            if parent_index == len(node_id_chain) - 2 and node_id_chain[-1] == node_id:
                return {"success": True, "messages": "already up to date"}
            self._set_item(
                key,
                node_id_chain[: parent_index + 1] + [node_id],
                STATIC_CACHE_DEFAULT_EXPIRE_SECONDS,
            )
        return {"success": True, "messages": "success"}

    @service_handler
    async def get_branch_chain(self, payload: dict) -> dict:
        """Read the branch chain for user_uid and conversation_uid.

        Returns success, messages=list and cache_hit=bool. An existing empty
        chain is a cache hit; missing or expired chains return [] and False."""
        key = build_conversation_key(payload, prefix="chain")
        async with self._lock:
            return self._read_result(key, deep=False)

    @service_handler
    async def set_messages_ttl(
        self, payload: dict, ttl_seconds: int = HOT_CACHE_DEFAULT_EXPIRE_SECONDS
    ) -> dict:
        """Set the TTL of cached messages for user_uid and conversation_uid.

        ttl_seconds defaults to HOT_CACHE_DEFAULT_EXPIRE_SECONDS.
        Returns success with messages="success", including on a cache miss."""
        key = build_conversation_key(payload)
        async with self._lock:
            item = self._get_item(key)
            if item is not None:
                item["expires_at"] = time.time() + ttl_seconds
        return {"success": True, "messages": "success"}

    @service_handler
    async def delete_messages(self, payload: dict) -> dict:
        """Remove cached messages for user_uid and conversation_uid.

        Returns success with messages="success", including on a cache miss.
        The conversation branch-chain cache is retained."""
        key = build_conversation_key(payload)
        async with self._lock:
            self._cache.pop(key, None)
        return {"success": True, "messages": "success"}

    # ------------------------------------------------------------------
    # Read-through Resource Cache
    # ------------------------------------------------------------------

    @service_handler
    async def set_resource(self, payload: dict) -> dict:
        """Cache a resource list with the static TTL.

        Payload requires nonempty cache_group and cache_key strings, plus a
        messages list. Empty lists are cached as hits.
        Returns success with messages="success"."""
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise TypeError("Resource messages must be a list")
        key = build_resource_key(payload)
        async with self._lock:
            self._set_item(
                key,
                copy.deepcopy(messages),
                STATIC_CACHE_DEFAULT_EXPIRE_SECONDS,
            )
        return {"success": True, "messages": "success"}

    @service_handler
    async def get_resource(self, payload: dict) -> dict:
        """Read the resource identified by cache_group and cache_key.

        Both keys must be nonempty strings. Returns success, messages=list and
        cache_hit=bool. A missing or expired entry returns [] and False."""
        key = build_resource_key(payload)
        async with self._lock:
            return self._read_result(key)

    @service_handler
    async def delete_resources(self, payload: dict) -> dict:
        """Remove one resource entry or all entries in a cache group.

        Payload requires a nonempty cache_group string. A non-None cache_key
        must be a nonempty string and selects one entry; absent/None removes
        the whole group. Returns success with messages="success"."""
        async with self._lock:
            if payload.get("cache_key") is not None:
                self._cache.pop(build_resource_key(payload), None)
            else:
                prefix = build_resource_prefix(payload)
                matching_keys = [key for key in self._cache if key.startswith(prefix)]
                for key in matching_keys:
                    self._cache.pop(key, None)
        return {"success": True, "messages": "success"}

    def _set_item(self, key: str, value: Any, ttl_seconds: int) -> None:
        self._cache[key] = {"value": value, "expires_at": time.time() + ttl_seconds}

    def _get_item(self, key: str) -> dict[str, Any] | None:
        item = self._cache.get(key)
        if item is None:
            return None
        if not item["expires_at"] > time.time():
            self._cache.pop(key, None)
            return None
        return item

    def _read_result(
        self, key: str, *, empty_is_miss: bool = False, deep: bool = True
    ) -> dict:
        """Build a disposable list response while the caller holds the lock."""
        item = self._get_item(key)
        if item is None or (empty_is_miss and not item["value"]):
            return {"success": True, "messages": [], "cache_hit": False}
        return {
            "success": True,
            "messages": copy.deepcopy(item["value"]) if deep else item["value"].copy(),
            "cache_hit": True,
        }

    def _purge_expired(self) -> None:
        now = time.time()
        self._cache = {
            key: item
            for key, item in self._cache.items()
            if self._is_valid_item(key, item, now)
        }

    @staticmethod
    def _is_valid_item(key: str, item: Any, now: float) -> bool:
        if not isinstance(item, dict):
            return False
        value = item.get("value")
        expires_at = item.get("expires_at")
        if (
            not isinstance(value, list)
            or type(expires_at) not in (int, float)
            or not now < expires_at < float("inf")
        ):
            return False
        if key.startswith("memo:"):
            return all(isinstance(message, dict) for message in value)
        if key.startswith("chain:"):
            return all(isinstance(node_id, str) for node_id in value)
        return key.startswith("resource:")
