"""Modus Desktop package.

Keep this package initializer dependency-free.  Event-contract tests and shared
message models must not import FastAPI/the server merely by importing a desktop
submodule.
"""


def start_server(*args, **kwargs):
    """Lazy public entry point for the FastAPI desktop server."""
    from modus.desktop.server import start_server as _start_server

    return _start_server(*args, **kwargs)


__all__ = ["start_server"]
