"""Tests for the test harness itself (section 1.10).

This is the part that is easy to skip and expensive to get wrong. The builder is
infrastructure, so if it is subtly wrong then every Phase 2 test is wrong in the
same direction and they all agree with one another — a whole suite confidently
green about a repository that does not exist.

So the builder is verified **by git**, never by our own parser. Ours does not
exist yet, and checking a git-writer with a git-reader we also wrote would be
circular.
"""

from __future__ import annotations

import time

import pytest

from tests.fixtures.gitrepo import (
    ALICE,
    BOB,
    CARLA,
    DEPENDABOT,
    REFERENCE_TIME,
    Author,
    Commit,
    build_stream,
    linear_history,
    run_git,
)
from tests.fixtures.golden import assert_golden, scrub


def log(repo, *args, reverse: bool = True) -> list[str]:
    """Ask git what it thinks is in the repository.

    ``reverse`` is a parameter rather than always on because ``--follow`` does
    not compose with ``--reverse``: git's rename following lives in the history
    walk, and reversing it silently returns only the commits after the rename.
    Forcing ``--reverse`` here made the rename test fail against a fixture that
    was perfectly correct.
    """
    output = run_git(["log", *(["--reverse"] if reverse else []), *args], cwd=repo)
    return [line for line in output.splitlines() if line]


# ---------------------------------------------------------------------------
# History, as git reports it
# ---------------------------------------------------------------------------
def test_commit_count_matches_the_specification(make_repo):
    repo = make_repo([Commit(f"change {n}", files={"a.py": f"v{n}\n"}) for n in range(7)])
    assert len(log(repo, "--format=%H")) == 7


def test_messages_authors_and_order_are_exact(make_repo):
    repo = make_repo(
        [
            Commit("first", author=ALICE, days_ago=30, files={"a.py": "1\n"}),
            Commit("second", author=BOB, days_ago=20, files={"a.py": "2\n"}),
            Commit("third", author=CARLA, days_ago=10, files={"a.py": "3\n"}),
        ]
    )
    assert log(repo, "--format=%s") == ["first", "second", "third"]
    assert log(repo, "--format=%an") == [ALICE.name, BOB.name, CARLA.name]
    assert log(repo, "--format=%ae") == [ALICE.email, BOB.email, CARLA.email]


def test_timestamps_are_exactly_what_was_asked_for(make_repo):
    """Section 2.5's decay is meaningless if the dates drift."""
    repo = make_repo([Commit("only", days_ago=90, files={"a.py": "x\n"})])
    (recorded,) = log(repo, "--format=%at")
    expected = int(REFERENCE_TIME.timestamp()) - 90 * 86400
    assert int(recorded) == expected


def test_author_and_committer_can_differ(make_repo):
    """Real histories separate these, and section 2.4 has to pick the right one."""
    repo = make_repo([Commit("patch", author=ALICE, committer=BOB, files={"a.py": "x\n"})])
    assert log(repo, "--format=%an") == [ALICE.name]
    assert log(repo, "--format=%cn") == [BOB.name]


def test_file_content_is_written_verbatim(make_repo):
    """core.autocrlf=false, or Windows would rewrite these bytes."""
    body = "def f():\n    return 1\n"
    repo = make_repo([Commit("add", files={"src/app.py": body})])
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == body


def test_later_commits_replace_earlier_content(make_repo):
    repo = make_repo(
        [
            Commit("first", files={"a.py": "one\n"}),
            Commit("second", files={"a.py": "two\n"}),
        ]
    )
    assert (repo / "a.py").read_text(encoding="utf-8") == "two\n"


def test_deletion_removes_the_file(make_repo):
    repo = make_repo(
        [
            Commit("add", files={"gone.py": "x\n", "kept.py": "y\n"}),
            Commit("remove", delete=["gone.py"]),
        ]
    )
    assert not (repo / "gone.py").exists()
    assert (repo / "kept.py").exists()


def test_renames_are_followed_by_git(make_repo):
    """Section 2.3 tracks file identity across renames; the fixture must support it."""
    repo = make_repo(
        [
            Commit("add", files={"old.py": "shared content\nline two\nline three\n"}),
            Commit("rename", rename={"old.py": "new.py"}),
        ]
    )
    assert (repo / "new.py").exists()
    assert not (repo / "old.py").exists()
    # reverse=False: --follow does not survive --reverse. See log().
    assert len(log(repo, "--follow", "--format=%s", "--", "new.py", reverse=False)) == 2


def test_merge_commits_have_two_parents(make_repo):
    """Section 2.2 excludes merges from churn, so it needs one to exclude."""
    repo = make_repo(
        [
            Commit("base", files={"a.py": "1\n"}),
            Commit("on a branch", branch="feature", files={"b.py": "2\n"}),
            Commit("back on main", files={"a.py": "3\n"}),
            Commit("merge the branch", merge="feature", files={}),
        ]
    )
    parents = run_git(["rev-list", "--merges", "--count", "HEAD"], cwd=repo).strip()
    assert parents == "1"


def test_binary_files_are_stored_as_bytes(make_repo):
    """numstat reports '-' for these, which section 2.2 must handle."""
    payload = bytes(range(256))
    repo = make_repo([Commit("add an image", binary={"logo.png": payload})])

    assert (repo / "logo.png").read_bytes() == payload
    numstat = run_git(["log", "--numstat", "--format=", "HEAD"], cwd=repo)
    assert numstat.strip().startswith("-\t-\t")


def test_bot_authors_are_representable(make_repo):
    """Section 2.4 must exclude them; a fixture has to be able to create them."""
    repo = make_repo([Commit("bump a dependency", author=DEPENDABOT, files={"lock": "x\n"})])
    assert "[bot]" in log(repo, "--format=%an")[0]


def test_mailmap_is_honoured_by_git(make_repo):
    """Section 2.4 collapses aliases; this proves the fixture can set that up."""
    typo = Author("Alice C.", "alice@old-domain.example")
    mailmap = f"{ALICE.name} <{ALICE.email}> <{typo.email}>\n"
    repo = make_repo(
        [
            Commit("add mailmap", author=ALICE, files={".mailmap": mailmap}),
            Commit("under an alias", author=typo, files={"a.py": "x\n"}),
        ]
    )
    assert log(repo, "--use-mailmap", "--format=%aN") == [ALICE.name, ALICE.name]


def test_paths_with_spaces_survive_quoting(make_repo):
    repo = make_repo([Commit("add", files={"src/my module/file name.py": "x\n"})])
    assert (repo / "src" / "my module" / "file name.py").exists()


def test_an_empty_history_still_produces_a_repository(make_repo):
    repo = make_repo([])
    assert (repo / ".git").is_dir()


# ---------------------------------------------------------------------------
# Determinism and speed
# ---------------------------------------------------------------------------
def test_the_same_specification_builds_identically(make_repo):
    """A fixture that varies between runs cannot anchor an assertion."""
    spec = [
        Commit("first", author=ALICE, days_ago=10, files={"a.py": "1\n"}),
        Commit("second", author=BOB, days_ago=5, files={"b.py": "2\n"}),
    ]
    first = make_repo(spec, name="one")
    second = make_repo(spec, name="two")

    fields = "--format=%H %an %ae %at %s"
    assert log(first, fields) == log(second, fields)


def test_the_stream_is_byte_identical_for_one_specification():
    spec = [Commit("only", files={"a.py": "x\n"})]
    assert build_stream(spec) == build_stream(spec)


def test_fifty_commits_build_quickly(make_repo):
    """The acceptance case, and the reason fast-import is used at all.

    Per-commit subprocesses would put this at three to five seconds on Windows.
    """
    commits = linear_history(files=["a.py", "b.py", "c.py"], commits=49)
    commits.append(Commit("rename", author=CARLA, days_ago=0, rename={"a.py": "renamed.py"}))

    started = time.perf_counter()
    repo = make_repo(commits)
    elapsed = time.perf_counter() - started

    assert len(log(repo, "--format=%H")) == 50
    assert len(set(log(repo, "--format=%ae"))) == 3
    assert (repo / "renamed.py").exists()
    assert elapsed < 2.0, f"took {elapsed:.2f}s; the target is under 1s"


def test_linear_history_spreads_commits_over_time(make_repo):
    repo = make_repo(linear_history(files=["a.py"], commits=10, start_days_ago=100))
    timestamps = [int(t) for t in log(repo, "--format=%at")]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] < timestamps[-1]


# ---------------------------------------------------------------------------
# Golden files
# ---------------------------------------------------------------------------
def test_scrub_replaces_volatile_content():
    text = "workspace legacy-monolith-a7f3k5m2 created 2026-06-01T12:00:00Z"
    cleaned = scrub(text)
    assert "<WORKSPACE_ID>" in cleaned
    assert "<TIMESTAMP>" in cleaned


def test_scrub_applies_literal_replacements_longest_first(tmp_path):
    """Otherwise a parent path rewrites part of its own child."""
    nested = f"{tmp_path}/inner"
    text = f"reading {nested}/file.py"
    cleaned = scrub(text, {str(tmp_path): "<TMP>", nested: "<NESTED>"})
    assert "<NESTED>/file.py" in cleaned


def test_scrub_normalises_line_endings():
    assert scrub("a\r\nb\r\n") == "a\nb\n"


def test_golden_creates_a_missing_file_and_says_so(tmp_path):
    with pytest.raises(AssertionError, match="has been created"):
        assert_golden("fresh", "some output\n", golden_dir=tmp_path)
    assert (tmp_path / "fresh.txt").read_text(encoding="utf-8") == "some output\n"


def test_golden_passes_on_a_match(tmp_path):
    (tmp_path / "matching.txt").write_text("expected\n", encoding="utf-8")
    assert_golden("matching", "expected\n", golden_dir=tmp_path)


def test_golden_failure_shows_a_diff(tmp_path):
    (tmp_path / "drift.txt").write_text("first line\nsecond line\n", encoding="utf-8")
    with pytest.raises(AssertionError) as excinfo:
        assert_golden("drift", "first line\nCHANGED\n", golden_dir=tmp_path)

    message = str(excinfo.value)
    assert "-second line" in message
    assert "+CHANGED" in message
    assert "--update-golden" in message


def test_golden_update_rewrites_the_file(tmp_path):
    (tmp_path / "stale.txt").write_text("old\n", encoding="utf-8")
    assert_golden("stale", "new\n", golden_dir=tmp_path, update=True)
    assert (tmp_path / "stale.txt").read_text(encoding="utf-8") == "new\n"


def test_golden_scrubs_before_comparing(tmp_path):
    (tmp_path / "scrubbed.txt").write_text("id <WORKSPACE_ID>\n", encoding="utf-8")
    assert_golden("scrubbed", "id legacy-monolith-a7f3k5m2\n", golden_dir=tmp_path)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
def test_home_fixture_is_isolated(home, tmp_path):
    assert home.is_relative_to(tmp_path)


def test_repo_fixture_looks_like_a_repository(repo):
    assert (repo / ".git").exists()
    assert " " in repo.name, "the space catches quoting bugs"


def test_workspace_fixture_has_no_database_yet(workspace):
    """Matches what create_workspace alone produces; scry init adds the database."""
    assert workspace.paths.marker.is_file()
    assert not workspace.paths.database.exists()


def test_initialised_workspace_fixture_has_one(initialised_workspace):
    assert initialised_workspace.paths.database.is_file()
