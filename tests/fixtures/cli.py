"""Driving the CLI from a test.

A plain function rather than a fixture, so the three command test modules can
share one implementation without every call site changing shape.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from pathlib import Path

from scry.cli.colors import strip_ansi
from scry.cli.router import run


class Result:
    """What one CLI invocation produced."""

    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.raw_out = out
        self.raw_err = err
        # Stripped by default: assertions should be about content, not escapes.
        self.out = strip_ansi(out)
        self.err = strip_ansi(err)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Result(code={self.code}, out={self.out!r}, err={self.err!r})"


def invoke(
    argv: Sequence[str],
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Result:
    """Run the CLI with captured streams and an isolated environment.

    ``env`` defaults to empty rather than to ``os.environ`` so a developer's own
    ``SCRY_*`` variables cannot change what the tests assert.
    """
    out, err = io.StringIO(), io.StringIO()
    code = run(argv, home=home, env={} if env is None else env, stdout=out, stderr=err)
    return Result(code, out.getvalue(), err.getvalue())


def snapshot(directory: Path) -> dict[str, bytes]:
    """Every file under ``directory`` with its bytes.

    Used to assert that Scry never writes into the repository it analyses — the
    guarantee is only meaningful if something checks it.
    """
    return {
        str(p.relative_to(directory)): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }
