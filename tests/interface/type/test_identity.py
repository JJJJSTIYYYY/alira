"""User operations accept base identities; conversation operations require a session."""

import pytest

from alira.agent.core.utils.funcs import check_conversation_identity, check_identity
from alira.common.type import IdentityError
from alira.interface.utils.identiity import AliraIdentity, ConversationIdentity
from alira.interface.utils.request import BasePayload, HttpRequest, WebSocketRequest


def test_user_and_conversation_validation():
    user: AliraIdentity = {"id": "user", "platform": "web"}
    conversation: ConversationIdentity = {**user, "conversation_uid": "conversation"}

    assert check_identity(user) == ("user", "web")
    assert check_identity(conversation) == ("user", "web")
    assert check_conversation_identity(conversation) == ("user", "web", "conversation")
    with pytest.raises(IdentityError, match="conversation_uid"):
        check_conversation_identity(user)


@pytest.mark.parametrize("validate", [check_identity, check_conversation_identity])
def test_identity_validation_rejects_missing_user_fields(validate):
    with pytest.raises(RuntimeError, match="not provided"):
        validate(None)
    with pytest.raises(IdentityError, match="platform"):
        validate({"id": "user"})
    with pytest.raises(IdentityError, match="user_uid"):
        validate({"platform": "web"})


@pytest.mark.parametrize("request_type", [HttpRequest, WebSocketRequest])
def test_requests_bind_user_identity_without_conversation(request_type):
    identity: AliraIdentity = {"id": "user", "platform": "web"}
    payload = BasePayload()
    options = {"action": "get_config"} if request_type is WebSocketRequest else {}

    request = request_type("request", identity, payload=payload, **options)

    assert request.identity is identity
    assert payload.identity is identity
    assert payload.request_id == "request"
