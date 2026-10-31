"""Conversation and resource namespaces for the store cache."""

from urllib.parse import quote


def build_conversation_key(payload: dict, prefix: str = "memo") -> str:
    """Build ``prefix:user_uid:conversation_uid``; both identifiers are required."""
    user_uid = payload.get("user_uid")
    conversation_uid = payload.get("conversation_uid")
    if not all([user_uid, conversation_uid]):
        raise KeyError("user_uid and conversation_uid required")
    return f"{prefix}:{user_uid}:{conversation_uid}"


def build_resource_key(payload: dict) -> str:
    """Append a URL-quoted, nonempty ``cache_key`` to its resource group prefix."""
    prefix = build_resource_prefix(payload)
    cache_key = payload.get("cache_key")
    if not isinstance(cache_key, str) or not cache_key:
        raise KeyError("non-empty cache_key required")
    return f"{prefix}{quote(cache_key, safe='')}"


def build_resource_prefix(payload: dict) -> str:
    """Build ``resource:quoted_cache_group:`` from a nonempty group string."""
    cache_group = payload.get("cache_group")
    if not isinstance(cache_group, str) or not cache_group:
        raise KeyError("non-empty cache_group required")
    return f"resource:{quote(cache_group, safe='')}:"
