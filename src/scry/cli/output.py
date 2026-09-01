"""Where output goes.

One rule, and everything else follows from it:

* **Command results go to stdout.**
* **Everything else — errors, warnings, progress, logs — goes to stderr.**

That is what makes ``scry hotspots --json | jq`` work, and what lets
``scry why x 2>/dev/null`` still print the answer. A test asserts stdout is
byte-empty when a command fails, because a single stray character there breaks
every caller that pipes us.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from scry.cli.colors import BOLD, RESET, SEMANTIC_COLORS


class Console:
    """Renders output, and knows which stream each kind belongs on."""

    def __init__(
        self,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        color: bool = False,
        json_mode: bool = False,
    ) -> None:
        self.stdout = sys.stdout if stdout is None else stdout
        self.stderr = sys.stderr if stderr is None else stderr
        self.color = color
        self.json_mode = json_mode

    # -- styling ----------------------------------------------------------
    def style(self, text: str, kind: str | None = None, *, bold: bool = False) -> str:
        if not self.color or (kind is None and not bold):
            return text
        prefix = SEMANTIC_COLORS.get(kind, "") if kind else ""
        if bold:
            prefix = BOLD + prefix
        return f"{prefix}{text}{RESET}" if prefix else text

    # -- results (stdout) -------------------------------------------------
    def out(self, text: str = "") -> None:
        print(text, file=self.stdout)

    def json(self, payload: Any) -> None:
        """Emit a machine-readable result.

        Sorted keys so piping into a diff is meaningful, and nothing else may
        reach stdout in this mode.
        """
        print(json.dumps(payload, indent=2, sort_keys=True), file=self.stdout)

    # -- messages (stderr) ------------------------------------------------
    def err(self, text: str = "") -> None:
        print(text, file=self.stderr)

    def error(self, text: str) -> None:
        label = self.style("error", "error", bold=True)
        first, *rest = str(text).splitlines() or [""]
        print(f"{label}: {first}", file=self.stderr)
        for line in rest:
            print(line, file=self.stderr)

    def warn(self, text: str) -> None:
        print(f"{self.style('warning', 'warning', bold=True)}: {text}", file=self.stderr)

    def hint(self, text: str) -> None:
        print(self.style(text, "muted"), file=self.stderr)
