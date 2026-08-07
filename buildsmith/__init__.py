"""Buildsmith — design and maintain Frappe Builder websites.

Everything lives under this one namespace on purpose: `primitives`, `workflows`
and `content` are all taken on PyPI, and a published package that claims a name
that generic would collide with somebody. It also means one import root for the
CLI, the TUI and anything embedding the library.

`buildsmith.primitives` and `buildsmith.workflows` have **no runtime
dependencies** and open no sockets. That is a structural property worth keeping:
tools here read files and write files, and something else applies the result.
"""

__version__ = "0.1.0"
