"""Transport requests bind user identity and request IDs to their payloads."""

import pytest

from alira.interface.request import HttpRequest, WebSocketRequest
from alira.shared.models.identity import AliraIdentity
from alira.shared.models.request import BasePayload


@pytest.mark.parametrize("request_type", [HttpRequest, WebSocketRequest])
def test_requests_bind_user_identity_without_conversation(request_type):
    identity: AliraIdentity = {"id": "user", "platform": "web"}
    payload = BasePayload()
    options = {"action": "get_config"} if request_type is WebSocketRequest else {}

    request = request_type("request", identity, payload=payload, **options)

    assert request.identity is identity
    assert payload.identity is identity
    assert payload.request_id == "request"
