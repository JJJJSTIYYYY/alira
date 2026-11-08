"""Stable database result codes and client-facing failure messages."""

from enum import IntEnum


class StoreCode(IntEnum):
    """Codes shared by query_store and the database services."""

    SUCCESS = 0
    INVALID_PAYLOAD = 1001
    UNKNOWN_ACTION = 1002
    USER_NOT_FOUND = 2001
    USER_ALREADY_EXISTS = 2002
    CONVERSATION_NOT_FOUND = 2003
    EMBEDDING_DISABLED = 2004
    SYNC_TASK_NOT_FOUND = 2005
    CONSTRAINT_VIOLATION = 3001
    DATABASE_ERROR = 3002
    FILE_ERROR = 3003
    STORE_UNAVAILABLE = 3004
    INTERNAL_ERROR = 5000


ERROR_MESSAGES: dict[StoreCode, str] = {
    StoreCode.INVALID_PAYLOAD: "Invalid request payload.",
    StoreCode.UNKNOWN_ACTION: "Unknown database action.",
    StoreCode.USER_NOT_FOUND: "User not found.",
    StoreCode.USER_ALREADY_EXISTS: "User name already exists.",
    StoreCode.CONVERSATION_NOT_FOUND: "Conversation not found or deleted.",
    StoreCode.EMBEDDING_DISABLED: "Message embedding is disabled.",
    StoreCode.SYNC_TASK_NOT_FOUND: "Message embedding sync task not found.",
    StoreCode.CONSTRAINT_VIOLATION: "Database constraint violation.",
    StoreCode.DATABASE_ERROR: "Database operation failed.",
    StoreCode.FILE_ERROR: "File operation failed.",
    StoreCode.STORE_UNAVAILABLE: "Database service is unavailable.",
    StoreCode.INTERNAL_ERROR: "Internal database service error.",
}


class StoreError(Exception):
    """A coded runtime or lifecycle exception; business rejections return results."""

    def __init__(self, code: StoreCode):
        self.code = code
        super().__init__(ERROR_MESSAGES[code])


def business_error_result(code: StoreCode) -> dict:
    """Return an expected business rejection without raising an exception.

    Args:
        code: A nonzero business error code from StoreCode.

    Returns:
        ``{"success": True, "code": int, "messages": str}``. The operation
        completed normally; code identifies the rejected business outcome.
    """
    return {"success": True, "code": int(code), "messages": ERROR_MESSAGES[code]}


def failure_result(code: StoreCode) -> dict:
    """Build a failure without exposing exception text or a traceback.

    Args:
        code: A failure member of StoreCode.

    Returns:
        ``{"success": False, "code": int, "messages": str}``, using the
        corresponding entry in ERROR_MESSAGES.
    """
    return {"success": False, "code": int(code), "messages": ERROR_MESSAGES[code]}


def exception_code(exc: Exception) -> StoreCode:
    """Classify an operation failure without including implementation details.

    Args:
        exc: Exception caught at a database operation boundary.

    Returns:
        The business code or a standard input, constraint, database, file or
        internal failure code. Cancellation is handled by the caller.
    """
    if isinstance(exc, StoreError):
        return exc.code
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return StoreCode.INVALID_PAYLOAD
    if isinstance(exc, OSError):
        return StoreCode.FILE_ERROR

    # Keep importing the public manager inert; SQLAlchemy belongs to the runtime.
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError

    if isinstance(exc, IntegrityError):
        return StoreCode.CONSTRAINT_VIOLATION
    if isinstance(exc, SQLAlchemyError):
        return StoreCode.DATABASE_ERROR
    return StoreCode.INTERNAL_ERROR
