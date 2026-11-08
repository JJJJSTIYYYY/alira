"""User and conversation identities shared across Alira modules."""

from typing import TypedDict


class AliraIdentity(TypedDict):
    """Identify an Alira user on a platform."""

    id: str  # user_uid
    name: str  # username
    platform: str


class ConversationIdentity(AliraIdentity):
    """Identify one conversation belonging to an Alira user."""

    conversation_uid: str


def key(identity: AliraIdentity) -> str:
    """Return a unique key for the identity."""
    conversation_uid = identity.get("conversation_uid")
    if conversation_uid:
        return f"{conversation_uid}:{identity['id']}:{identity['platform']}"
    return f"{identity['id']}:{identity['platform']}"
