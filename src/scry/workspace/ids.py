"""Workspace names and identifiers.

An id looks like ``legacy-monolith-a7f3k9m2``: a validated name, a hyphen, and
eight characters of lowercase base32.

Base32 is chosen for a property that matters to something people retype from a
terminal: the RFC 4648 alphabet omits ``0``, ``1``, ``8`` and ``9``, so there is
no ``0``/``O`` or ``1``/``l`` confusion anywhere in an id.
"""

from __future__ import annotations

import base64
import re
import secrets
import time

from scry.util.errors import WorkspaceError

MIN_NAME_LENGTH = 3
MAX_NAME_LENGTH = 50
UID_LENGTH = 8

# Segments of lowercase alphanumerics joined by single hyphens. Written this way
# rather than as a character class so that leading, trailing and doubled hyphens
# are rejected by construction instead of by three extra checks.
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# The awkward brace escaping is confined to one fragment so the composed
# patterns below stay readable.
_UID_FRAGMENT = rf"[a-z2-7]{{{UID_LENGTH}}}"
UID_PATTERN = re.compile(rf"^{_UID_FRAGMENT}$")
ID_PATTERN = re.compile(rf"^(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<uid>{_UID_FRAGMENT})$")

# Windows cannot create a directory with any of these names, and fails in a way
# that has nothing to do with what the user typed. Rejecting them here turns a
# mystifying filesystem error into a sentence that names the problem.
RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


def validate_workspace_name(name: str) -> str:
    """Return the normalised form of ``name``, or raise :class:`WorkspaceError`.

    Names are lowercased so that behaviour is identical on every platform.
    Windows filesystems are case-insensitive, so ``MyProject`` and ``myproject``
    are the same directory there and different ones on Linux; normalising
    removes that discrepancy rather than leaving it to surprise someone.
    """
    if not isinstance(name, str):
        raise WorkspaceError(f"workspace name must be text, got {type(name).__name__}")

    normalized = name.strip().lower()

    if not normalized:
        raise WorkspaceError("workspace name is empty")

    if not (MIN_NAME_LENGTH <= len(normalized) <= MAX_NAME_LENGTH):
        raise WorkspaceError(
            f"workspace name must be {MIN_NAME_LENGTH}-{MAX_NAME_LENGTH} characters, "
            f"got {len(normalized)}: {normalized!r}"
        )

    if normalized in RESERVED_NAMES:
        raise WorkspaceError(
            f"{normalized!r} is a reserved device name on Windows and cannot be a "
            f"directory. Choose a different workspace name."
        )

    if not NAME_PATTERN.match(normalized):
        raise WorkspaceError(
            f"invalid workspace name {normalized!r}. Use lowercase letters, digits and "
            f"single hyphens between them, for example 'legacy-monolith'."
        )

    return normalized


def generate_uid(*, now: float | None = None) -> str:
    """Return eight lowercase base32 characters: 20 bits of time, 20 of randomness.

    Forty bits packs into exactly five bytes, which base32-encodes to exactly
    eight characters with no padding. The time half gives ids a rough creation
    order, which makes a directory listing readable; the random half keeps two
    workspaces made in the same second apart.

    Twenty random bits is not much on its own, and it does not need to be:
    :func:`scry.workspace.manager.create_workspace` checks whether the directory
    already exists and regenerates if so. Collisions are handled by looking
    rather than by making the number bigger.
    """
    timestamp = int(time.time() if now is None else now)
    time_bits = timestamp & 0xFFFFF
    random_bits = secrets.randbits(20)
    packed = ((time_bits << 20) | random_bits).to_bytes(5, "big")
    return base64.b32encode(packed).decode("ascii").lower()


def generate_workspace_id(name: str, *, now: float | None = None) -> str:
    """Return ``{validated-name}-{uid}``."""
    return f"{validate_workspace_name(name)}-{generate_uid(now=now)}"


def parse_workspace_id(workspace_id: str) -> tuple[str, str] | None:
    """Split an id into ``(name, uid)``, or return None if it is not id-shaped.

    Note that this is a shape test, not an existence test. A name such as
    ``my-projects`` parses as name ``my`` plus uid ``projects``, because the
    trailing segment happens to be eight characters from the base32 alphabet.
    Resolution therefore looks for a matching directory first and only falls
    back to a name search, so the ambiguity resolves itself.
    """
    match = ID_PATTERN.match(workspace_id)
    return (match["name"], match["uid"]) if match else None


def looks_like_workspace_id(token: str) -> bool:
    return ID_PATTERN.match(token) is not None
