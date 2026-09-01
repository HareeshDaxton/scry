"""Command handlers, one module each.

Every module here exposes::

    def add_arguments(parser: ArgumentParser) -> None:   # optional
    def run(args: Namespace, ctx: Context) -> int:

Modules are imported only when their command is selected, so a heavy import in
one command never slows another down. See ``scry.cli.registry``.
"""
