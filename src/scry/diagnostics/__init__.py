"""Environment and workspace diagnostics.

A separate package rather than something inside ``cli/commands`` because more
than one caller wants it: ``scry doctor`` today, section 6.1's backend detection
next, and section 1.12's Conductor health rules after that.
"""

from scry.diagnostics.checks import (
    BACKEND,
    ENVIRONMENT,
    GROUP_ORDER,
    RESOURCES,
    STORAGE,
    WORKSPACES,
    CheckResult,
    Diagnosis,
    Status,
    repair,
    run_checks,
)

__all__ = [
    "BACKEND",
    "ENVIRONMENT",
    "GROUP_ORDER",
    "RESOURCES",
    "STORAGE",
    "WORKSPACES",
    "CheckResult",
    "Diagnosis",
    "Status",
    "repair",
    "run_checks",
]
