"""JSON hooks preserve nested dataclasses and use Pydantic's JSON encoding."""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from pydantic import BaseModel

from alira.utils.serialization import json_default


class ProviderRecord(BaseModel):
    created_on: date
    cost: Decimal


@dataclass(slots=True)
class Record:
    name: str
    metadata: dict


@dataclass
class Envelope:
    provider: ProviderRecord
    records: list[Record]


def test_pydantic_values_use_json_mode():
    record = ProviderRecord(created_on=date(2026, 11, 8), cost=Decimal("1.25"))

    assert json_default(record) == {"created_on": "2026-11-08", "cost": "1.25"}
    assert record.created_on == date(2026, 11, 8)
    assert record.cost == Decimal("1.25")


def test_dataclass_projection_borrows_existing_field_values():
    metadata = {"nested": ["value"]}
    record = Record("上海", metadata)

    result = json_default(record)

    assert result == {"name": "上海", "metadata": metadata}
    assert result["metadata"] is metadata


def test_json_encoder_recursively_handles_mixed_typed_objects():
    envelope = Envelope(
        ProviderRecord(created_on=date(2026, 11, 8), cost=Decimal("0.00")),
        [Record("上海", {"enabled": False})],
    )

    encoded = json.dumps({"origin": envelope}, default=json_default, ensure_ascii=False)

    assert "上海" in encoded
    assert json.loads(encoded) == {
        "origin": {
            "provider": {"created_on": "2026-11-08", "cost": "0.00"},
            "records": [{"name": "上海", "metadata": {"enabled": False}}],
        }
    }


@pytest.mark.parametrize("value", [object(), {1, 2}, b"bytes"])
def test_unsupported_values_raise_instead_of_becoming_strings(value):
    with pytest.raises(TypeError, match=f"{type(value).__name__} is not JSON serializable"):
        json.dumps({"value": value}, default=json_default)
