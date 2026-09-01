"""Golden-file comparison for command output.

Used by section 2.11's ``scry why`` and friends, and by section 3.6, where the
Onboarding Brief must render byte-identically across runs.

The non-obvious part is **scrubbing**. Real output carries temporary paths,
timestamps and workspace ids that differ on every run, so a naive golden file
fails immediately and for the wrong reason. Volatile content is replaced with
placeholders before comparison, leaving only what the test actually means to
assert.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"

# Applied to every comparison. Anything genuinely unpredictable belongs here, so
# individual tests do not each have to remember it.
DEFAULT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ISO timestamps as written by util.clock
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"), "<TIMESTAMP>"),
    # Workspace ids: a name plus eight base32 characters
    (re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]+)*-[a-z2-7]{8}\b"), "<WORKSPACE_ID>"),
    # Commit shas
    (re.compile(r"\b[0-9a-f]{40}\b"), "<SHA>"),
    (re.compile(r"\b[0-9a-f]{7,12}\b(?![.\w])"), "<SHORT_SHA>"),
)


def scrub(text: str, replacements: Mapping[str, str] | None = None) -> str:
    """Replace volatile content with stable placeholders.

    Literal replacements are applied longest-first, so a nested path is not
    partially rewritten by its own parent.
    """
    result = text.replace("\r\n", "\n")

    for literal in sorted(replacements or {}, key=len, reverse=True):
        result = result.replace(literal, (replacements or {})[literal])

    for pattern, placeholder in DEFAULT_PATTERNS:
        result = pattern.sub(placeholder, result)

    return result


def assert_golden(
    name: str,
    actual: str,
    *,
    replacements: Mapping[str, str] | None = None,
    update: bool = False,
    golden_dir: Path | None = None,
) -> None:
    """Compare ``actual`` against a stored expectation.

    Run pytest with ``--update-golden`` to rewrite the files after a deliberate
    change. On a mismatch the failure shows a unified diff, because two walls of
    text side by side tell you nothing.
    """
    directory = golden_dir or GOLDEN_DIR
    path = directory / f"{name}.txt"
    cleaned = scrub(actual, replacements)

    if update or not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(cleaned, encoding="utf-8", newline="\n")
        if not update:
            raise AssertionError(
                f"golden file {path} did not exist and has been created. Review it, then re-run."
            )
        return

    expected = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if expected == cleaned:
        return

    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            cleaned.splitlines(),
            fromfile=f"{path} (expected)",
            tofile="actual",
            lineterm="",
        )
    )
    raise AssertionError(
        f"output does not match {path}\n\n{diff}\n\n"
        f"If this change is intended, re-run with --update-golden."
    )
