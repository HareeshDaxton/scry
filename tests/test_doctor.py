"""Tests for ``scry doctor`` (section 1.9).

The rule under most scrutiny is that no check may raise. Doctor runs precisely
when something is already broken, so a doctor that crashes while diagnosing
turns one confusing failure into two.
"""

from __future__ import annotations

import io
import json

import pytest

from scry.cli.colors import strip_ansi
from scry.cli.router import run
from scry.diagnostics import Status, repair, run_checks
from scry.diagnostics.checks import (
    WORKSPACES,
    CheckResult,
    _safe,
    check_backend,
    check_config,
    check_git,
    check_home,
    check_python,
    check_workspaces,
)
from scry.diagnostics.system import check_writable, human_bytes
from scry.storage import initialise_database
from scry.util.errors import ExitCode
from scry.util.logging import reset_logging
from scry.workspace import create_workspace


@pytest.fixture(autouse=True)
def isolated_logging():
    reset_logging()
    yield
    reset_logging()


@pytest.fixture
def home(tmp_path):
    return tmp_path / "scry_home"


@pytest.fixture
def repo(tmp_path):
    directory = tmp_path / "repo"
    (directory / "src").mkdir(parents=True)
    (directory / ".git").mkdir()
    return directory


@pytest.fixture
def workspace(home, repo):
    created = create_workspace("demo-project", repo, home=home)
    initialise_database(created.paths.database)
    return created


class Result:
    def __init__(self, code, out, err):
        self.code = code
        self.out = strip_ansi(out)
        self.err = strip_ansi(err)
        self.raw_out = out


def invoke(argv, *, home) -> Result:
    out, err = io.StringIO(), io.StringIO()
    code = run(argv, home=home, env={}, stdout=out, stderr=err)
    return Result(code, out.getvalue(), err.getvalue())


def status_of(diagnosis, name) -> Status:
    return next(r.status for r in diagnosis.results if r.name == name)


# ---------------------------------------------------------------------------
# The never-raise rule
# ---------------------------------------------------------------------------
def test_a_check_that_raises_becomes_a_failure():
    def explode():
        raise RuntimeError("the check is broken")

    (result,) = _safe("Environment", "exploding", explode)
    assert result.status is Status.FAIL
    assert "the check is broken" in result.detail
    assert "bug in Scry" in (result.remedy or "")


def test_one_broken_check_does_not_stop_the_others(home, monkeypatch):
    """The whole point: a broken install must still produce a full report."""
    import scry.diagnostics.checks as checks

    def explode():
        raise OSError("no such device")

    monkeypatch.setattr(checks, "check_memory", explode)
    diagnosis = run_checks(home=home)

    assert status_of(diagnosis, "memory") is Status.FAIL
    assert status_of(diagnosis, "python") is Status.OK
    assert len(diagnosis.results) > 5


def test_doctor_survives_an_unreadable_home(tmp_path):
    """A file where a directory belongs: every storage check should fail, not crash."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    diagnosis = run_checks(home=blocker / "home")
    assert diagnosis.status is Status.FAIL


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def test_python_check_passes_on_a_supported_interpreter():
    assert check_python().status is Status.OK


def test_git_check_reports_the_version():
    result = check_git()
    assert result.status is Status.OK
    assert "git version" in result.detail


def test_git_check_fails_when_git_is_absent(monkeypatch):
    import scry.diagnostics.checks as checks

    monkeypatch.setattr(checks, "git_version", lambda: (False, "not found on PATH"))
    result = check_git()
    assert result.status is Status.FAIL
    assert "PATH" in (result.remedy or "")


def test_home_check_passes_on_a_writable_directory(home):
    assert check_home(home).status is Status.OK


def test_home_check_fails_when_the_path_is_a_file(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    result = check_home(blocker / "home")
    assert result.status is Status.FAIL
    assert "SCRY_HOME" in (result.remedy or "")


def test_writability_is_tested_by_writing(tmp_path):
    """os.access lies on Windows: it reports the attribute and ignores ACLs."""
    ok, detail = check_writable(tmp_path / "fresh")
    assert ok and detail == "writable"
    assert not any((tmp_path / "fresh").iterdir()), "the probe file was left behind"


def test_missing_config_is_not_a_problem(tmp_path):
    """Section 1.2: absent means 'use the defaults', which is a normal state."""
    result = check_config(tmp_path / "absent.yaml", {})
    assert result.status is Status.OK
    assert "defaults" in result.detail


def test_unknown_config_keys_warn(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("skeptic:\n  challange_threshold: 0.9\n", encoding="utf-8")
    result = check_config(path, {})
    assert result.status is Status.WARN
    assert "unrecognised" in result.detail


def test_invalid_config_fails_with_the_reason(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("skeptic:\n  batch_size: ten\n", encoding="utf-8")
    result = check_config(path, {})
    assert result.status is Status.FAIL
    assert "batch_size" in result.detail


def test_a_missing_backend_is_reported_as_ok_not_a_warning():
    """Lite is a supported mode. Yellow would teach users they are degraded."""
    result = check_backend()
    assert result.status is Status.OK
    assert "lite mode" in result.detail


def test_human_bytes_formats_both_scales():
    assert human_bytes(4 * 1024**3) == "4.0 GB"
    assert human_bytes(None) == "unknown"


# ---------------------------------------------------------------------------
# Workspace diagnosis
# ---------------------------------------------------------------------------
def test_a_healthy_workspace_reports_no_problems(home, workspace):
    results = check_workspaces(home)
    assert all(r.status is Status.OK for r in results)
    assert "1 complete" in results[0].detail


def test_an_incomplete_workspace_is_detected(home, workspace):
    """A tree with no marker is what an interrupted creation leaves behind."""
    workspace.paths.marker.unlink()
    results = check_workspaces(home)

    failures = [r for r in results if r.status is Status.FAIL]
    assert failures
    assert "creation was interrupted" in failures[0].detail
    assert str(workspace.root) in (failures[0].remedy or "")


def test_a_corrupt_marker_is_detected(home, workspace):
    workspace.paths.marker.write_text("{ not json", encoding="utf-8")
    failures = [r for r in check_workspaces(home) if r.status is Status.FAIL]
    assert any("not valid JSON" in r.detail for r in failures)


def test_a_missing_database_is_detected(home, workspace):
    """The gap section 1.8 deliberately leaves open."""
    workspace.paths.database.unlink()
    failures = [r for r in check_workspaces(home) if r.status is Status.FAIL]
    assert any("database is missing" in r.detail for r in failures)
    assert any("--repair" in (r.remedy or "") for r in failures)


def test_an_orphaned_target_is_detected(home, repo, workspace):
    """Someone deletes or moves the repository the workspace points at."""
    import shutil

    shutil.rmtree(repo)
    failures = [r for r in check_workspaces(home) if r.status is Status.FAIL]
    assert any("target no longer exists" in r.detail for r in failures)


def test_a_corrupt_database_is_reported_not_raised(home, workspace):
    workspace.paths.database.write_bytes(b"this is not a database")
    failures = [r for r in check_workspaces(home) if r.status is Status.FAIL]
    assert failures, "a corrupt database should be reported"


def test_several_problems_are_all_reported(home, repo, tmp_path):
    """Doctor should not stop at the first fault."""
    broken = create_workspace("no-database", repo, home=home)
    broken.paths.database.unlink(missing_ok=True)

    incomplete = create_workspace("interrupted", repo, home=home)
    incomplete.paths.marker.unlink()

    failures = [r for r in check_workspaces(home) if r.status is Status.FAIL]
    assert len(failures) >= 2


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------
def test_repair_creates_a_missing_database(home, workspace):
    workspace.paths.database.unlink()
    actions = repair(home=home)

    assert any("created missing database" in a for a in actions)
    assert workspace.paths.database.exists()
    assert all(r.status is Status.OK for r in check_workspaces(home))


def test_repair_recreates_missing_directories(home, workspace):
    import shutil

    shutil.rmtree(workspace.paths.exports)
    repair(home=home)
    assert workspace.paths.exports.is_dir()


def test_repair_is_idempotent(home, workspace):
    assert repair(home=home) == []


def test_repair_never_deletes_an_incomplete_workspace(home, workspace):
    """Additive only. A diagnostic that can silently delete analysis is not trustworthy."""
    workspace.paths.marker.unlink()
    repair(home=home)
    assert workspace.root.is_dir(), "repair removed a workspace directory"


def test_repair_never_writes_into_the_target_repository(home, repo, workspace):
    before = {p.name for p in repo.rglob("*")}
    workspace.paths.database.unlink()
    repair(home=home)
    assert {p.name for p in repo.rglob("*")} == before


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------
def test_doctor_passes_on_a_healthy_install(home, workspace):
    result = invoke(["doctor"], home=home)
    assert result.code == ExitCode.OK
    assert "No problems found." in result.out


def test_doctor_fails_on_a_broken_workspace(home, workspace):
    workspace.paths.database.unlink()
    result = invoke(["doctor"], home=home)
    assert result.code == ExitCode.ERROR
    assert "problem(s) found" in result.out


def test_doctor_groups_its_output(home):
    out = invoke(["doctor"], home=home).out
    for group in ("Environment", "Resources", "Storage", "Backend", "Workspaces"):
        assert group in out


def test_doctor_prints_remedies_for_problems(home, workspace):
    workspace.paths.database.unlink()
    assert "-> " in invoke(["doctor"], home=home).out


def test_warnings_alone_do_not_fail(home, monkeypatch):
    """'3.9 GB of RAM' should not break somebody's CI."""
    import scry.diagnostics.checks as checks

    monkeypatch.setattr(
        checks,
        "check_cpus",
        lambda: CheckResult("Resources", "cpus", Status.WARN, "only one"),
    )
    result = invoke(["doctor"], home=home)
    assert result.code == ExitCode.OK
    assert "warning(s)" in result.out


def test_doctor_repair_fixes_and_then_passes(home, workspace):
    workspace.paths.database.unlink()
    assert invoke(["doctor"], home=home).code == ExitCode.ERROR

    result = invoke(["doctor", "--repair"], home=home)
    assert result.code == ExitCode.OK
    assert "Repaired" in result.out
    assert "No problems found." in result.out


def test_json_output_is_valid_and_alone_on_stdout(home, workspace):
    result = invoke(["--json", "doctor"], home=home)
    payload = json.loads(result.raw_out)

    assert payload["status"] == "ok"
    assert payload["repaired"] == []
    names = {check["name"] for check in payload["checks"]}
    assert {"python", "git", "platform", "cpus", "scry home", "llm backend"} <= names


def test_json_reports_failures_and_repairs(home, workspace):
    workspace.paths.database.unlink()
    payload = json.loads(invoke(["--json", "doctor", "--repair"], home=home).raw_out)

    assert payload["status"] == "ok"
    assert any("created missing database" in action for action in payload["repaired"])


def test_doctor_honours_an_explicit_config_path(home, tmp_path):
    """--config must reach the check, or doctor reports on the wrong file."""
    config_file = tmp_path / "custom.yaml"
    config_file.write_text("skeptic:\n  batch_size: ten\n", encoding="utf-8")

    payload = json.loads(
        invoke(["--json", "--config", str(config_file), "doctor"], home=home).raw_out
    )
    configuration = next(c for c in payload["checks"] if c["name"] == "configuration")
    assert configuration["status"] == "fail"


def test_doctor_appears_in_the_command_list(home):
    assert "doctor" in invoke([], home=home).out


def test_doctor_reports_an_empty_home_without_failing(tmp_path):
    """A first run, before any workspace exists."""
    result = invoke(["doctor"], home=tmp_path / "brand_new")
    assert result.code == ExitCode.OK
    assert "0 complete" in result.out


def test_workspaces_group_appears_even_when_empty(home):
    diagnosis = run_checks(home=home)
    assert any(r.group == WORKSPACES for r in diagnosis.results)
