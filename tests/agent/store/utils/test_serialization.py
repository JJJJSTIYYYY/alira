"""Database timestamp normalization."""

from alira.database.utils.serialization import normalize_row


def test_normalize_row_timestamps():
    assert normalize_row(
        {
            "created_at": "2026-01-02 03:04:05",
            "timestamp": "2026-01-02 03:04:06",
            "content": "leave me alone",
        }
    ) == {
        "created_at": "2026-01-02T03:04:05",
        "timestamp": "2026-01-02T03:04:06",
        "content": "leave me alone",
    }
