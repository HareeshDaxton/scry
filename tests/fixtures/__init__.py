"""Shared test infrastructure.

Importable as ``tests.fixtures.*`` because ``pythonpath = ["."]`` is set in the
pytest configuration — which section 1.5 added so spawned worker processes could
import the test module, and which pays off again here.
"""
