"""Numbered SQL migrations, applied in order and forward only.

Files are named ``NNN_description.sql`` and loaded through
``importlib.resources``, which is the correct API for package data and behaves
identically in an editable install and a built wheel. If a migration ever fails
to be packaged, that breaks in the test suite rather than on a user's machine
after ``pip install``.
"""
