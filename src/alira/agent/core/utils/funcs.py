from contextlib import contextmanager
from datetime import datetime
import json
import time
from typing import Callable, Generator, Literal

from alira.common.type import IdentityError
from alira.common.type.identiity import AliraIdentity, ConversationIdentity


def get_date_natural_language() -> str:
    '''Get current date in natural language.

    Example: "Wednesday, April 15th, 2026"
    '''

    import inflect

    now = datetime.now()
    
    day = now.day
    month = now.strftime("%B")
    year = now.year
    weekday = now.strftime("%A")
    
    p = inflect.engine()
    ordinal_day = p.ordinal(day)
    
    natural_date = f"DATE: {weekday}, {month} {ordinal_day}, {year}"
    
    return natural_date


def convert_generation_id_to_message_node_id(
    generation_id: str | list[str] | set[str],
    role: Literal['user', 'assistant', 'assistant', 'system', 'tool', 'info']
) -> str | list[str] | set[str]:
    '''Convert generation id to message node id.
    '''

    suffix = "-user" if role == 'user' else "-alira"

    def convert(gid: str) -> str:
        return gid[-12:] + suffix

    if isinstance(generation_id, str):
        return convert(generation_id)

    return type(generation_id)(
        convert(gid)
        for gid in generation_id
    )


def check_identity(identity: AliraIdentity | None) -> tuple[str, str]:
    """Validate a user identity and return ``(user_uid, platform)``.

    Raise RuntimeError for a missing identity, or IdentityError for empty fields.
    """
    if not identity:
        raise RuntimeError("Alira identity is not provided.")
    
    raise_key = []
    if not identity.get("id"):
        raise_key.append('user_uid')
    if not identity.get("platform"):
        raise_key.append("platform")
    if raise_key:
        raise IdentityError(f"Key {", ".join(raise_key)} is not provided in identity.")
    
    return identity["id"], identity["platform"]


def check_conversation_identity(
    identity: ConversationIdentity | None,
) -> tuple[str, str, str]:
    """Validate a conversation identity and return its user, platform and session."""
    user_uid, platform = check_identity(identity)
    if identity is None or not identity.get("conversation_uid"):
        raise IdentityError("Key conversation_uid is not provided in identity.")
    return user_uid, platform, identity["conversation_uid"]


@contextmanager
def timer(name: str = "CodeBlock", callback: Callable = None) -> Generator[None, None, None]:
    """
    Context manager that automatically prints the execution time of a code block.

    Args:
        name: Name of the timed block, used for log output.

    Yields:
        Start timestamp.

    Example:
        with timer("Database query"):
            result = db.query()
    """
    start = time.perf_counter()  # Use high-precision monotonic timer
    yield start
    elapsed = time.perf_counter() - start
    if callback:
        callback(elapsed = elapsed)
