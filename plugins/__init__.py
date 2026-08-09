"""Optional tool plugins.

Each subdirectory here is a plugin: an ``__init__.py`` exposing a ``TOOL``
manifest dict and a FastAPI ``router`` (see ``core.plugins``). Plugins run as
trusted, same-process Python — only place code you control in this directory.
"""
