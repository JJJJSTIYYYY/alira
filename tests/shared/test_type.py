"""Unavailable module values preserve enough context for callers to act on them."""

import pytest

from alira.shared import ModuleNotLoaded, ModuleNotLoadedError


def test_unavailable_value_keeps_module_identity_when_used_in_boolean_context():
    value = ModuleNotLoaded("tool")

    assert not value
    assert value is not None
    assert value.module_name == "tool"


def test_unavailable_module_error_preserves_context_for_handlers():
    value = ModuleNotLoaded("database")

    with pytest.raises(RuntimeError) as caught:
        raise ModuleNotLoadedError(value.module_name)

    assert caught.value.module_name == "database"
    assert "database" in str(caught.value)
