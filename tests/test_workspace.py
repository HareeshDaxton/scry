"""Tests for the workspace model (section 1.4).

The invariant worth singling out is
``test_creating_a_workspace_never_touches_the_target_repository``. Scry's whole
premise is analysing code you may not own and may not have write access to, so
"we only ever read your repository" has to be a tested property rather than an
intention.
"""

from __future__ import annotations

import json
import os

import pytest

from scry.util.errors import ExitCode, WorkspaceError
from scry.util.paths import scry_home
from scry.workspace import (
    MARKER_FILENAME,
    MARKER_SCHEMA_VERSION,
    RESERVED_NAMES,
    UID_LENGTH,
    WorkspaceMarker,
    WorkspacePaths,
    create_workspace,
    find_incomplete_workspaces,
    generate_uid,
    generate_workspace_id,
    list_workspaces,
    parse_workspace_id,
    resolve_workspace,
    validate_workspace_name,
    workspaces_dir,
)
from tests.fixtures.cli import snapshot

# `home` and `repo` come from conftest, shared with the command test modules.


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------
def test_generated_id_has_the_documented_shape():
    workspace_id = generate_workspace_id("legacy-monolith")
    name, uid = parse_workspace_id(workspace_id)
    assert name == "legacy-monolith"
    assert len(uid) == UID_LENGTH


def test_uid_avoids_visually_confusable_characters():
    """Base32 omits 0, 1, 8 and 9, so there is no 0/O or 1/l confusion to retype."""
    uids = "".join(generate_uid() for _ in range(200))
    assert not set(uids) & set("0189")
    assert set(uids) <= set("abcdefghijklmnopqrstuvwxyz234567")


def test_ids_for_the_same_name_are_distinct():
    ids = {generate_workspace_id("same-name") for _ in range(50)}
    assert len(ids) > 1


def test_uid_encodes_time_so_ids_sort_roughly_by_creation():
    early = generate_uid(now=1_000_000)
    later = generate_uid(now=9_000_000)
    assert early[:2] != later[:2]


@pytest.mark.parametrize(
    "not_an_id",
    [
        "demo",  # no uid segment at all
        "demo-proj",  # final segment too short
        "demo-project0",  # 0 is not in the base32 alphabet
        "demo-monolith1",  # final segment too long
        "",
    ],
)
def test_parse_rejects_things_that_are_not_id_shaped(not_an_id: str):
    assert parse_workspace_id(not_an_id) is None


def test_a_plain_name_can_look_id_shaped(home, repo):
    """`legacy-monolith` parses as id-shaped, because `monolith` is eight
    characters from the base32 alphabet. That ambiguity is unavoidable, so
    resolution looks for a matching directory first and only then searches by
    name — which makes it resolve correctly anyway.
    """
    assert parse_workspace_id("legacy-monolith") == ("legacy", "monolith")

    workspace = create_workspace("legacy-monolith", repo, home=home)
    assert resolve_workspace("legacy-monolith", home=home).id == workspace.id


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------
def test_names_are_normalised_to_lowercase():
    """Windows filesystems are case-insensitive; normalising removes the discrepancy."""
    assert validate_workspace_name("MyProject") == "myproject"
    assert validate_workspace_name("  spaced  ") == "spaced"


@pytest.mark.parametrize("reserved", sorted(RESERVED_NAMES))
def test_windows_reserved_device_names_are_refused(reserved: str):
    """Without this check these fail as a mystifying filesystem error instead."""
    with pytest.raises(WorkspaceError, match="reserved device name"):
        validate_workspace_name(reserved)


@pytest.mark.parametrize(
    "bad",
    [
        "ab",  # too short
        "x" * 51,  # too long
        "-leading",
        "trailing-",
        "double--hyphen",
        "under_score",
        "has space",
        "trailing.dot.",
        "unicode-Ω",
        "",
    ],
)
def test_invalid_names_are_refused(bad: str):
    with pytest.raises(WorkspaceError):
        validate_workspace_name(bad)


def test_name_error_explains_the_rule():
    with pytest.raises(WorkspaceError) as excinfo:
        validate_workspace_name("Not_Valid")
    assert "legacy-monolith" in str(excinfo.value), "error should show a valid example"


def test_workspace_errors_use_the_workspace_not_found_exit_code():
    assert WorkspaceError("x").exit_code == ExitCode.WORKSPACE_NOT_FOUND


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------
def test_creates_the_full_directory_tree(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    for directory in workspace.paths.directories():
        assert directory.is_dir(), f"{directory} was not created"
    assert workspace.paths.marker.is_file()


def test_config_yaml_is_not_created(home, repo):
    """Section 1.2: a missing config means 'use defaults', never 'write one'."""
    workspace = create_workspace("demo-project", repo, home=home)
    assert not workspace.paths.config.exists()


def test_creating_a_workspace_never_touches_the_target_repository(home, repo):
    before = snapshot(repo)
    create_workspace("demo-project", repo, home=home)
    assert snapshot(repo) == before, "Scry wrote into the repository it was analysing"


def test_workspace_lives_under_scry_home_not_in_the_repo(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    assert workspace.root.is_relative_to(workspaces_dir(home))
    assert not workspace.root.is_relative_to(repo)


def test_marker_records_the_resolved_target(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    assert workspace.target_path == repo.resolve()


def test_paths_with_spaces_work(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    assert " " in str(repo)
    assert resolve_workspace(workspace.id, home=home).id == workspace.id


def test_same_name_twice_yields_two_distinct_workspaces(home, repo):
    first = create_workspace("twice", repo, home=home)
    second = create_workspace("twice", repo, home=home)
    assert first.id != second.id
    assert first.root != second.root
    assert len(list_workspaces(home)) == 2


def test_missing_target_is_refused(home, tmp_path):
    with pytest.raises(WorkspaceError, match="does not exist"):
        create_workspace("demo-project", tmp_path / "nope", home=home)


def test_target_that_is_a_file_is_refused(home, tmp_path):
    a_file = tmp_path / "a-file.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="not a directory"):
        create_workspace("demo-project", a_file, home=home)


def test_unknown_mode_is_refused(home, repo):
    with pytest.raises(WorkspaceError, match="unknown mode"):
        create_workspace("demo-project", repo, mode="turbo", home=home)


def test_reserved_name_is_refused_before_touching_the_filesystem(home, repo):
    with pytest.raises(WorkspaceError, match="reserved device name"):
        create_workspace("nul", repo, home=home)
    assert not workspaces_dir(home).exists() or not any(workspaces_dir(home).iterdir())


# ---------------------------------------------------------------------------
# The marker
# ---------------------------------------------------------------------------
def test_marker_round_trips(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    reread = WorkspaceMarker.read(workspace.paths.marker)
    assert reread == workspace.marker


def test_marker_is_json_with_the_documented_fields(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    data = json.loads(workspace.paths.marker.read_text(encoding="utf-8"))
    assert set(data) == {"schema_version", "id", "name", "target_path", "created_at", "mode"}
    assert data["schema_version"] == MARKER_SCHEMA_VERSION
    assert data["created_at"].endswith("Z")


def test_marker_ignores_unknown_fields(home, repo):
    """A marker written by a later build with the same schema must still open."""
    workspace = create_workspace("demo-project", repo, home=home)
    data = json.loads(workspace.paths.marker.read_text(encoding="utf-8"))
    data["some_future_field"] = {"nested": True}
    workspace.paths.marker.write_text(json.dumps(data), encoding="utf-8")
    assert WorkspaceMarker.read(workspace.paths.marker).id == workspace.id


def test_marker_from_a_newer_schema_is_refused_clearly(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    data = json.loads(workspace.paths.marker.read_text(encoding="utf-8"))
    data["schema_version"] = MARKER_SCHEMA_VERSION + 99
    workspace.paths.marker.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="newer version of Scry"):
        WorkspaceMarker.read(workspace.paths.marker)


def test_corrupt_marker_names_the_file(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    workspace.paths.marker.write_text("{ not json", encoding="utf-8")
    with pytest.raises(WorkspaceError) as excinfo:
        WorkspaceMarker.read(workspace.paths.marker)
    assert MARKER_FILENAME in str(excinfo.value)
    assert "not valid JSON" in str(excinfo.value)


def test_marker_missing_required_fields_is_refused(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    workspace.paths.marker.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(WorkspaceError, match="missing required field"):
        WorkspaceMarker.read(workspace.paths.marker)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def test_resolve_by_exact_id(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    assert resolve_workspace(workspace.id, home=home).id == workspace.id


def test_resolve_by_unambiguous_name(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    assert resolve_workspace("demo-project", home=home).id == workspace.id


def test_resolve_by_name_is_case_insensitive(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    assert resolve_workspace("DEMO-PROJECT", home=home).id == workspace.id


def test_ambiguous_name_lists_the_candidates(home, repo):
    """Silently picking one would answer questions about the wrong repository."""
    first = create_workspace("twice", repo, home=home)
    second = create_workspace("twice", repo, home=home)
    with pytest.raises(WorkspaceError) as excinfo:
        resolve_workspace("twice", home=home)
    message = str(excinfo.value)
    assert first.id in message
    assert second.id in message


def test_unknown_token_lists_what_does_exist(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    with pytest.raises(WorkspaceError) as excinfo:
        resolve_workspace("no-such-thing", home=home)
    assert workspace.id in str(excinfo.value)


def test_unknown_token_with_no_workspaces_suggests_init(home):
    with pytest.raises(WorkspaceError, match="scry init"):
        resolve_workspace("anything", home=home)


def test_resolve_from_the_target_directory(home, repo):
    workspace = create_workspace("demo-project", repo, home=home)
    assert resolve_workspace(cwd=repo, home=home).id == workspace.id


def test_resolve_from_a_nested_subdirectory(home, repo):
    """`scry why` must work from deep inside the tree, not only at its root."""
    workspace = create_workspace("demo-project", repo, home=home)
    assert resolve_workspace(cwd=repo / "src", home=home).id == workspace.id


def test_most_specific_target_wins(home, tmp_path):
    outer = tmp_path / "work"
    inner = outer / "repo"
    inner.mkdir(parents=True)
    create_workspace("outer-ws", outer, home=home)
    inner_ws = create_workspace("inner-ws", inner, home=home)
    assert resolve_workspace(cwd=inner, home=home).id == inner_ws.id


def test_resolution_by_cwd_uses_platform_case_rules(home, repo):
    """Case-insensitive on Windows, case-sensitive on POSIX — as each FS behaves."""
    create_workspace("demo-project", repo, home=home)
    shouty = repo.parent / repo.name.upper()
    if os.path.normcase("A") == os.path.normcase("a"):
        assert resolve_workspace(cwd=shouty, home=home).name == "demo-project"
    else:
        with pytest.raises(WorkspaceError):
            resolve_workspace(cwd=shouty, home=home)


def test_unrelated_directory_resolves_to_nothing(home, repo, tmp_path):
    create_workspace("demo-project", repo, home=home)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(WorkspaceError, match="no workspace covers"):
        resolve_workspace(cwd=elsewhere, home=home)


# ---------------------------------------------------------------------------
# Listing and incomplete workspaces
# ---------------------------------------------------------------------------
def test_listing_is_empty_before_anything_exists(home):
    assert list_workspaces(home) == []


def test_listing_returns_every_workspace(home, repo):
    created = {create_workspace(f"proj-{n}", repo, home=home).id for n in range(3)}
    assert {w.id for w in list_workspaces(home)} == created


def test_a_tree_without_a_marker_is_incomplete_not_listed(home, repo):
    """Creation writes the marker last, so this is the shape of an interrupted run."""
    create_workspace("demo-project", repo, home=home)
    orphan = workspaces_dir(home) / "interrupted-aaaa2222"
    orphan.mkdir()

    assert orphan not in [w.root for w in list_workspaces(home)]
    assert orphan in find_incomplete_workspaces(home)


def test_one_corrupt_marker_does_not_break_the_listing(home, repo):
    good = create_workspace("good-one", repo, home=home)
    broken = create_workspace("broken-one", repo, home=home)
    broken.paths.marker.write_text("{ not json", encoding="utf-8")

    warnings: list[str] = []
    listed = list_workspaces(home, on_warning=warnings.append)

    assert [w.id for w in listed] == [good.id]
    assert warnings and "not valid JSON" in warnings[0]


# ---------------------------------------------------------------------------
# Home resolution
# ---------------------------------------------------------------------------
def test_scry_home_honours_the_environment_override(tmp_path):
    assert scry_home(env={"SCRY_HOME": str(tmp_path)}) == tmp_path


def test_scry_home_defaults_under_the_user_home():
    assert scry_home(env={}).name == ".scry"


def test_workspace_paths_derive_everything_from_the_root(tmp_path):
    paths = WorkspacePaths(root=tmp_path / "ws")
    assert paths.marker.parent == paths.root
    assert paths.cache_ast.is_relative_to(paths.cache)
    assert paths.exports.parent == paths.root
