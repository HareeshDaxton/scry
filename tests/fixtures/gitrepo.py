"""Builds git repositories with exactly the history a test asserts against.

Every Phase 2 correctness test rests on this. Archivist tests need statements
like "this file changed 42 times, by 3 authors, one of whom stopped committing
14 months ago, and it was renamed twice" — and no real repository can supply
that. Real repositories gain commits, take seconds to clone, and their ground
truth has to be hand-verified once and then trusted forever. A test whose
expected value drifts is worse than no test.

**Built with ``git fast-import``.** The obvious implementation — ``git add`` and
``git commit`` per commit — costs one subprocess each, roughly 20-40 ms on
Windows, which puts a fifty-commit repository at three to five seconds. Section
2.2 will want ten thousand commits to test constant-memory streaming.
``fast-import`` consumes a description of an entire history and creates it in one
process, which makes fifty commits a fraction of a second.

**Built with real git**, rather than by writing loose objects directly. Writing
objects ourselves would be faster still, but if our encoding differed subtly from
git's — tree ordering, mode bits, index details — we would produce repositories
our tests read correctly and real git reads differently, and every Phase 2 test
would pass against something that does not exist.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

# A fixed instant, so a repository built twice is identical.
#
# Times are expressed as `days_ago` relative to this rather than as absolute
# dates, for a reason that will bite section 2.5: churn decays with a 90-day
# half-life relative to *now*. A fixture pinned to an absolute date would make
# every decay assertion drift with the calendar, passing today and failing in
# three months for no discoverable reason. Section 2.5's churn computation must
# therefore accept an injectable clock, and tests pass this same instant to both
# sides.
REFERENCE_TIME = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

DEFAULT_BRANCH = "main"
FILE_MODE = "100644"


@dataclass(frozen=True)
class Author:
    name: str
    email: str

    def stamp(self, when: datetime) -> str:
        return f"{self.name} <{self.email}> {int(when.timestamp())} +0000"


ALICE = Author("Alice Chen", "alice@example.com")
BOB = Author("Bob Silva", "bob@example.com")
CARLA = Author("Carla Ortiz", "carla@example.com")

# Section 2.4 must exclude these by default: left in, automated accounts dominate
# churn and ownership on most real repositories.
DEPENDABOT = Author("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")
GITHUB_ACTIONS = Author("github-actions[bot]", "github-actions[bot]@users.noreply.github.com")


@dataclass(frozen=True)
class Commit:
    """One commit in a synthetic history.

    ``files`` sets paths to the given content, whether or not they already exist
    — git makes no distinction between adding and modifying, so neither does
    this.
    """

    message: str
    author: Author = ALICE
    days_ago: float = 0.0
    files: Mapping[str, str] = field(default_factory=dict)
    binary: Mapping[str, bytes] = field(default_factory=dict)
    delete: Sequence[str] = ()
    rename: Mapping[str, str] = field(default_factory=dict)
    committer: Author | None = None
    branch: str = DEFAULT_BRANCH
    # Name of another branch to merge in, producing a two-parent commit.
    merge: str | None = None


class GitUnavailableError(RuntimeError):
    """Raised when git is not installed, so callers can skip rather than fail."""


def git_is_available() -> bool:
    return shutil.which("git") is not None


def run_git(args: Sequence[str], *, cwd: Path, stdin: bytes | None = None) -> str:
    if not git_is_available():
        raise GitUnavailableError("git is not installed")
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({result.returncode}):\n"
            f"{result.stderr.decode('utf-8', 'replace')}"
        )
    return result.stdout.decode("utf-8", "replace")


def _quote(path: str) -> str:
    """fast-import path, quoted only when it needs to be."""
    if any(character in path for character in ' "\\\n'):
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return path


def _data(payload: bytes) -> bytes:
    """A fast-import ``data`` block. The length is in *bytes*, not characters."""
    return f"data {len(payload)}\n".encode() + payload + b"\n"


def build_stream(commits: Sequence[Commit], *, reference: datetime = REFERENCE_TIME) -> bytes:
    """Render commits as a ``git fast-import`` stream."""
    out = bytearray()
    marks = count(1)
    branch_heads: dict[str, int] = {}

    for commit in commits:
        when = reference - timedelta(days=commit.days_ago)
        blobs: list[tuple[str, int]] = []

        for path, text in commit.files.items():
            mark = next(marks)
            out += b"blob\n" + f"mark :{mark}\n".encode() + _data(text.encode("utf-8"))
            blobs.append((path, mark))

        for path, payload in commit.binary.items():
            mark = next(marks)
            out += b"blob\n" + f"mark :{mark}\n".encode() + _data(payload)
            blobs.append((path, mark))

        commit_mark = next(marks)
        committer = commit.committer or commit.author

        out += f"commit refs/heads/{commit.branch}\n".encode()
        out += f"mark :{commit_mark}\n".encode()
        out += f"author {commit.author.stamp(when)}\n".encode()
        out += f"committer {committer.stamp(when)}\n".encode()
        out += _data(commit.message.encode("utf-8"))

        # `from` is needed only to start a branch. fast-import tracks the head of
        # a ref it has already written, so a linear history needs it once.
        if commit.branch not in branch_heads:
            parent = branch_heads.get(DEFAULT_BRANCH)
            if parent is not None:
                out += f"from :{parent}\n".encode()

        if commit.merge is not None:
            other = branch_heads.get(commit.merge)
            if other is None:
                raise ValueError(f"cannot merge unknown branch {commit.merge!r}")
            out += f"merge :{other}\n".encode()

        for old, new in commit.rename.items():
            out += f"R {_quote(old)} {_quote(new)}\n".encode()
        for path in commit.delete:
            out += f"D {_quote(path)}\n".encode()
        for path, mark in blobs:
            out += f"M {FILE_MODE} :{mark} {_quote(path)}\n".encode()

        branch_heads[commit.branch] = commit_mark

    out += b"done\n"
    return bytes(out)


def build_repo(
    path: Path,
    commits: Sequence[Commit],
    *,
    reference: datetime = REFERENCE_TIME,
) -> Path:
    """Create a repository at ``path`` containing exactly ``commits``."""
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "--quiet", f"--initial-branch={DEFAULT_BRANCH}"], cwd=path)

    # Without this, Windows checkouts rewrite line endings and every content
    # assertion in Phase 2 would compare against mangled bytes.
    run_git(["config", "core.autocrlf", "false"], cwd=path)

    if commits:
        run_git(["fast-import", "--quiet", "--done"], cwd=path, stdin=build_stream(commits, reference=reference))
        run_git(["reset", "--hard", DEFAULT_BRANCH], cwd=path)

    return path


# ---------------------------------------------------------------------------
# Convenience shapes
# ---------------------------------------------------------------------------
def linear_history(
    *,
    files: Sequence[str],
    commits: int,
    authors: Sequence[Author] = (ALICE, BOB, CARLA),
    start_days_ago: float = 365.0,
    message: str = "change {n}",
) -> list[Commit]:
    """A straightforward history touching ``files`` in rotation.

    Commits are spaced evenly from ``start_days_ago`` up to the reference
    instant, so tests can reason about recency without inventing dates.
    """
    if commits < 1:
        return []
    step = start_days_ago / commits
    return [
        Commit(
            message=message.format(n=n),
            author=authors[n % len(authors)],
            days_ago=start_days_ago - (n * step),
            files={files[n % len(files)]: f"# revision {n}\nvalue = {n}\n"},
        )
        for n in range(commits)
    ]
