"""User operations accept base identities; conversation operations require a session."""

import pytest

from alira.shared.models.identity import (
    AliraIdentity,
    ConversationIdentity,
)
from alira.utils.validation import validate_typed_dict_fields


def test_user_and_conversation_required_fields():
    user: AliraIdentity = {"id": "user", "platform": "web"}
    conversation: ConversationIdentity = {**user, "conversation_uid": "conversation"}

    validate_typed_dict_fields(user, AliraIdentity, non_empty=True)
    validate_typed_dict_fields(conversation, AliraIdentity, non_empty=True)
    validate_typed_dict_fields(conversation, ConversationIdentity, non_empty=True)
    with pytest.raises(ValueError, match="conversation_uid"):
        validate_typed_dict_fields(user, ConversationIdentity, non_empty=True)


@pytest.mark.parametrize(
    "schema", [AliraIdentity, ConversationIdentity]
)
def test_identity_fields_validation_rejects_missing_user_fields(schema):
    with pytest.raises(ValueError, match="platform"):
        validate_typed_dict_fields({"id": "user"}, schema, non_empty=True)
    with pytest.raises(ValueError, match="id"):
        validate_typed_dict_fields({"platform": "web"}, schema, non_empty=True)
