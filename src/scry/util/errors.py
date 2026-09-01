"""Scry's exception taxonomy and process exit codes.

Every error Scry raises deliberately derives from :class:`ScryError`, so the CLI
can distinguish "we detected and explained a problem" from "we crashed", and
render the former as a clean message and exit code rather than a traceback.

Exit codes live on the exception classes rather than in a mapping table in the
CLI. A table can silently forget a new exception type — someone adds
``IndexerError`` in phase 7, misses the table, and it falls through to a generic
code or an unhandled traceback. With the code on the class, a new subclass
inherits a sensible default automatically and overriding it is a deliberate
one-line decision at the point of definition.

Section 1.7 therefore reduces to::

    except ScryError as exc:
        log.error("%s", exc)
        return exc.exit_code
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import IntEnum
from typing import Any, ClassVar, Final


class ExitCode(IntEnum):
    """Process exit codes. Section 1.7 maps the CLI onto these."""

    OK = 0
    ERROR = 1
    USAGE = 2
    WORKSPACE_NOT_FOUND = 3
    GUARDRAIL = 4


class _Unset:
    """Sentinel for 'no value supplied', distinct from a legitimate ``None``."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "<unset>"


UNSET: Final = _Unset()


class ScryError(Exception):
    """Base class for every error Scry raises deliberately."""

    exit_code: ClassVar[int] = ExitCode.ERROR


class ConfigError(ScryError):
    """Configuration could not be read, parsed, or validated.

    The rendered message names three things: where the value came from, which
    key it was, and what was expected versus what was found. A config error
    that says only "invalid value" forces the user to hunt through layered
    files to work out which one is at fault, which is precisely the experience
    this class exists to prevent.

    Example output::

        C:\\Users\\me\\.scry\\config.yaml: skeptic.batch_size
          expected  integer >= 1
          got       'ten'  (str)
    """

    def __init__(
        self,
        key: str,
        *,
        source: str,
        expected: str | None = None,
        got: Any = UNSET,
        detail: str | None = None,
    ) -> None:
        self.key = key
        self.source = source
        self.expected = expected
        self.has_got = not isinstance(got, _Unset)
        self.got = got if self.has_got else None
        self.detail = detail
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [f"{self.source}: {self.key}"]
        if self.expected is not None:
            lines.append(f"  expected  {self.expected}")
        if self.has_got:
            lines.append(f"  got       {self.got!r}  ({type(self.got).__name__})")
        if self.detail:
            lines.append(f"  {self.detail}")
        return "\n".join(lines)


class WorkspaceError(ScryError):
    """A workspace is missing, malformed, or ambiguous.

    Carries its own exit code so a script can distinguish "no such workspace"
    from a general failure without parsing the message.
    """

    exit_code: ClassVar[int] = ExitCode.WORKSPACE_NOT_FOUND

    def __init__(self, message: str, *, path: Any = None) -> None:
        self.path = path
        super().__init__(f"{message}\n  path      {path}" if path else message)


class StorageError(ScryError):
    """The session database or knowledge graph could not be read or written."""

    def __init__(self, message: str, *, path: Any = None) -> None:
        self.path = path
        super().__init__(f"{message}\n  database  {path}" if path else message)


class GitError(ScryError):
    """A git invocation failed, or the repository is unusable.

    Records the command and its stderr because section 2.1 needs both: a git
    failure with neither is nearly impossible to diagnose from a log.
    """

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str] | None = None,
        stderr: str | None = None,
    ) -> None:
        self.command: tuple[str, ...] | None = tuple(command) if command else None
        self.stderr = stderr
        lines = [message]
        if self.command:
            lines.append(f"  command   {' '.join(self.command)}")
        if stderr and stderr.strip():
            lines.append(f"  stderr    {stderr.strip()}")
        super().__init__("\n".join(lines))


class AgentError(ScryError):
    """An agent failed in a way its own retry logic could not absorb."""

    def __init__(self, message: str, *, agent: str | None = None) -> None:
        self.agent = agent
        super().__init__(f"[{agent}] {message}" if agent else message)


class SecurityError(ScryError):
    """A guardrail refused an operation.

    Distinct exit code: a guardrail refusal is a deliberate, correct outcome,
    not a malfunction, and callers should be able to tell the difference.
    """

    exit_code: ClassVar[int] = ExitCode.GUARDRAIL
