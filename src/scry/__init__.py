"""Scry — terminal-native agentic AI for software archaeology.

Keep this module import-cheap. It runs on every ``import scry``, including
fast paths such as ``scry why`` and ``scry owners``, which are expected to
answer in well under a second. Do not import submodules here, and do not do
work at module scope.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
