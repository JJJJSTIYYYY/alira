from typing import TypedDict


class AliraIdentity(TypedDict):
    """Identify an Alira user on a platform."""

    id: str
    platform: str


class ConversationIdentity(AliraIdentity):
    """Identify one conversation belonging to an Alira user."""

    conversation_uid: str
