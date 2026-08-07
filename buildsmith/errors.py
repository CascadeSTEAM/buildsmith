"""The exception that means "this check never ran".

Exit codes are a contract in this repo: `0` proved, `1` found a problem,
`2` could not check. The third exists because "I could not verify this"
must never read as "this is fine" — and, before this module, tools raised
`SystemExit` with a message, which the CLI mapped to exit 1. That was the
safe direction but still a lie: a caller scripting on the 1-vs-2
distinction reads "could not check" as "found a problem" and goes hunting
for a defect that does not exist.

Raise :class:`CouldNotCheck` for absent preconditions — no crawl to compare,
no baseline checkpoint, no export, the sandbox not running. ``cli.main()``
prints the message and exits 2. A genuine finding (the artifact is wrong)
stays what it always was: a message and exit 1.
"""

from __future__ import annotations

EXIT_OK, EXIT_PROBLEM, EXIT_UNCHECKED = 0, 1, 2

__all__ = ["CouldNotCheck", "EXIT_OK", "EXIT_PROBLEM", "EXIT_UNCHECKED"]


class CouldNotCheck(SystemExit):
    """A check could not run at all. Exit 2 — never mistakable for 0 or 1.

    Subclasses SystemExit so an escape that somehow misses the CLI's handler
    still refuses (non-zero) instead of passing — the same property
    `CannotProve` (optimize) established; that class is now a subclass of
    this one, so one `except CouldNotCheck` catches both.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
