from typing import Any, NotRequired, TypedDict


# Basic type for 
class AliraIdentity(TypedDict):
    id: str
    platform: str
    conversation_uid: str
    associated_account: NotRequired[dict] # platform: uid


# Data schema for alira system entry point
class AliraEntryDataSchema(TypedDict):
    target: AliraIdentity
    action: str
    data: Any