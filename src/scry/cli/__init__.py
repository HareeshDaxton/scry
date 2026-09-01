"""Command-line interface: argument parsing, subcommand routing, rendering.

Everything in this package sits on the always-loaded path, so it stays on
stdlib only. Leaf dependencies (Jinja2, NetworkX, textual) must be imported
inside the functions that need them — see ``tests/test_import_hygiene.py``.
"""
