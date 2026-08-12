"""A `page_data_script` that lists records safely — the other half of a
data-driven page (issue #11): a menu, a price list, a roster.

TRAP-020 is why this module exists rather than a site author writing the
script by hand. Builder's `page_data_script` runs in **its own** sandbox
(`builder/utils.py`'s `safer_exec`), not a Frappe Server Script's — and the
two disagree about basics:

- `frappe.get_list`/`frappe.get_all` **do not exist** in it at all. Using
  them raises `AttributeError` — loudly, and identically for the page's
  author previewing it and for a real visitor. Forgiving, not the trap.
- `frappe.db.get_list` exists and is **permission-checked as whoever is
  viewing the page**. An author previewing the page is logged in and sees
  data every time; a real website visitor is Guest, who has no read
  permission on almost any doctype by default, and the whole page 500s for
  them. This is the trap: it looks finished to the person who built it.
- `frappe.db.get_all` exists and **always sets `ignore_permissions=True`**
  (`builder/utils.py`'s `safe_get_all`) — the right default for content
  that is *meant* to be public, and needs no DocPerm setup on the doctype at
  all. This module only ever emits calls to it, on purpose.
- A field name containing `(` is silently dropped from the query
  (`remove_unsafe_fields`) — no error, just a missing column. Refused here
  at build time instead, loudly, before it ever reaches Builder's sandbox.

`list_data_script()` builds the script text via `repr()`, the same safe
pattern `check_traps.py`'s bench scripts use to embed data into generated
Python — not string interpolation, so nothing here can break out of its own
literal. Pair its output with `primitives.repeater.repeater(data_key=target,
...)`, which already speaks `comesFrom: "dataScript"` — nothing new needed
there.

Nothing here touches a site, and nothing here touches the network.
"""

from __future__ import annotations

import keyword
import re

from buildsmith.primitives.blocks import BlockError

__all__ = ["DataPageError", "list_data_script"]


class DataPageError(BlockError):
    """A data-driven page's script would fail at Builder's sandbox, or silently drop data."""


#: Ordinary Frappe fieldnames. `remove_unsafe_fields` (builder/utils.py) only
#: excludes names containing `(`, but that is a denylist — it says nothing
#: about what a fieldname actually looks like. An allowlist refuses more,
#: loudly, before Builder's sandbox has a chance to drop anything silently.
_FIELDNAME = re.compile(r"^[a-z][a-z0-9_]*$")

#: JSON-safe container/scalar types — everything `repr()` round-trips as
#: valid Python literal syntax, and nothing whose repr() could be arbitrary
#: code (a custom object, a function, a set with unhashable contents).
_SAFE_SCALARS = (str, int, float, bool, type(None))


def _assert_json_safe(value, *, path: str) -> None:
    if isinstance(value, _SAFE_SCALARS):
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _assert_json_safe(item, path=f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DataPageError(f"{path}: dict keys must be strings, got {key!r}")
            _assert_json_safe(item, path=f"{path}[{key!r}]")
        return
    raise DataPageError(
        f"{path}: {value!r} ({type(value).__name__}) cannot be safely embedded in a "
        "generated script — only str/int/float/bool/None/list/dict, so `repr()` always "
        "round-trips as valid Python."
    )


def list_data_script(
    *,
    target: str,
    doctype: str,
    fields: list[str],
    filters: dict | list | None = None,
    order_by: str | None = None,
) -> str:
    """Build a `page_data_script` that lists `doctype` records as `data.<target>`.

    Always emits `frappe.db.get_all(...)` — never `.get_list`, which is
    permission-checked against Guest and would 500 the page for every real
    visitor while working perfectly for whoever is previewing it (TRAP-020).
    `get_all` defaults to no page-size limit (`limit_page_length=0` when
    unset, in `builder/utils.py`'s `safe_get_all`), so a 50-item menu is not
    silently truncated to Frappe's ordinary default of 20.

    Pair the `target` with `primitives.repeater.repeater(data_key=target,
    ...)` on the page's own blocks — that is what actually reads `data.
    <target>` back out at render time.
    """
    if not target or not target.isidentifier() or keyword.iskeyword(target):
        raise DataPageError(
            f"target={target!r} becomes the attribute `data.{target}`, so it must be a "
            "valid Python identifier and not a keyword."
        )
    if not doctype or not doctype.strip():
        raise DataPageError("doctype must be non-empty.")
    if not fields:
        raise DataPageError(
            "fields must list at least one column — an empty list asks Builder's "
            "sandbox for nothing, which is indistinguishable from every record being "
            "hidden by a permission you did not intend (TRAP-020)."
        )
    bad = [f for f in fields if not _FIELDNAME.match(f)]
    if bad:
        raise DataPageError(
            f"field(s) {bad!r} do not look like plain Frappe fieldnames. In "
            "particular, Builder's sandbox silently drops any field containing '(' "
            "(remove_unsafe_fields) — refusing here, loudly, rather than shipping a "
            "script that quietly returns fewer columns than it asks for."
        )

    _assert_json_safe(fields, path="fields")
    if filters is not None:
        _assert_json_safe(filters, path="filters")

    kwargs = [f"fields={fields!r}"]
    if filters is not None:
        kwargs.append(f"filters={filters!r}")
    if order_by:
        kwargs.append(f"order_by={order_by!r}")

    return f"data.{target} = frappe.db.get_all({doctype!r}, {', '.join(kwargs)})"
