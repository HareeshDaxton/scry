"""Timestamps.

One format everywhere: ISO 8601 in UTC with a trailing ``Z``. Text rather than
epoch integers because it sorts correctly as text, and because it is readable
when someone opens the database in a SQLite browser to work out what went wrong
— which is exactly when you least want to be decoding epoch seconds.
"""

from __future__ import annotations

from datetime import UTC, datetime

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)
