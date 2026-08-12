# Data-driven pages — Frappe records feeding a Builder page

The pattern for content that is really a list — a menu, a price list, a
staff roster — and changes often enough that hand-editing Builder blocks
every time is the wrong tool. Instead: the content lives as ordinary Frappe
records, and the page reads them at render time. Editing happens in the
desk UI, not the page editor.

Tracks GitHub issue #11. Everything below is generic and already merged to
`main`; nothing here is specific to any one site.

---

## The pattern, in three pieces

1. **A custom Frappe doctype** holding the records (e.g. a menu item: name,
   category, price). Plain fields, no special setup.
2. **A `page_data_script`** on the `Builder Page` — server-side Python,
   sandboxed by Builder itself, that queries the doctype and assigns the
   result onto `data`.
3. **A repeater block** on the page, bound to that data, rendering one card
   per record.

None of this needs a rebuild or a redeploy to reflect a content change —
only steps 1 and 2 are set up once. After that, adding, editing, or removing
a record in the desk UI is immediately live on the page.

## Prerequisites

Nothing extra to install — the capability ships in `buildsmith` itself
(`primitives/datapage.py`, `primitives/repeater.py`, `primitives/template.py`).
What you need on the **target site**:

- The content doctype must exist there. Buildsmith never creates a doctype
  on a live site itself (see `AGENTS.md`'s actions-vs-artifacts boundary) —
  that's an action, performed through an OpsKit subagent for a real site, or
  directly for local dogfooding against `buildsmith sandbox up`.
- Confirm the site's bench does **not** have Server Scripts enabled
  (`common_site_config.json`) before relying on the public-by-default
  behaviour below — see the caveat under Known traps.

## Building one

**1. Design the doctype.** Fields only, no permission ceremony —
`frappe.db.get_all` (see below) works for a public page without granting
Guest read on the doctype:

```json
{
  "doctype": "DocType",
  "name": "Menu Item",
  "module": "Builder",
  "custom": 1,
  "naming_rule": "Autoincrement",
  "autoname": "autoincrement",
  "fields": [
    {"fieldname": "item_name", "fieldtype": "Data", "label": "Item Name", "reqd": 1},
    {"fieldname": "category", "fieldtype": "Data", "label": "Category", "reqd": 1},
    {"fieldname": "price", "fieldtype": "Data", "label": "Price", "reqd": 1}
  ],
  "permissions": [
    {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}
  ]
}
```

**2. Build the `page_data_script`** with `primitives.datapage.list_data_script()`
— never by hand (see Known traps for why):

```python
from buildsmith.primitives.datapage import list_data_script

script = list_data_script(
    target="menu_items",           # becomes data.menu_items
    doctype="Menu Item",
    fields=["item_name", "price"],
    filters={"category": "Basics"},
    order_by="idx",
)
```

If the page groups records under section headings (e.g. one `<h2>` per
category), call `list_data_script()` once per category and join the lines —
Builder's repeater has no "group by," so each heading needs its own query
and its own repeater block underneath it. **Each call needs its own
`target`.** `list_data_script()` has no awareness of any other call in the
same script, so two categories sharing a `target` (e.g. both left at
`"items"`) silently produce `data.items = ...` twice — the second
assignment overwrites the first, every repeater bound to that key renders
the *last* category's rows under every heading, and nothing errors.
Deriving the target from the category itself (`f"items_{slugify(cat)}"`)
makes a collision structurally hard to reach by accident.
`primitives.repeater.repeater()`'s `data_key` already speaks the right
vocabulary (`comesFrom: "dataScript"`) when given a plain string, so no
extra wiring is needed there.

**3. Build the repeater block** that reads it:

```python
from buildsmith.primitives.blocks import new_block
from buildsmith.primitives.repeater import binding, repeater

card = new_block("div", children=[
    new_block("span", inner_html="Item", dynamic_values=[binding("item_name", "innerHTML")]),
    new_block("span", inner_html="$0.00", dynamic_values=[binding("price", "innerHTML")]),
])
grid = repeater(data_key="menu_items", element="div", child=card)
```

**4. Wire it into the page.** `page_data_script` is a first-class field on
`primitives.template.Page` — pass it to `page()`, or set it directly in a
`design/pages/*.json` spec (it is plain Python text, never `@token`-resolved,
unlike `blocks`):

```json
{
  "title": "Menu",
  "route": "menu",
  "blocks": [ /* the repeater block(s), plus whatever else the page needs */ ],
  "published": true,
  "page_data_script": "data.menu_items = frappe.db.get_all(...)"
}
```

Then the ordinary sequence:

```sh
buildsmith build --site <site>
buildsmith validate --site <site>
buildsmith load --site <site>       # local sandbox only — see AGENTS.md
```

## Optional fields and conditional sub-lists

Not every record needs every field filled in, and a second, smaller
section reading a *filtered subset* of the same doctype is a normal
extension of the pattern — no new mechanism, just a second
`list_data_script()` call with a stricter filter.

Example: a boolean "featured" flag plus an optional image, where the
embellished treatment only applies once *both* are true — flagged but not
yet photographed just renders normally, nowhere else:

```python
featured_script = list_data_script(
    target="featured_items",
    doctype="Menu Item",
    fields=["item_name", "price", "image"],
    filters=[["is_featured", "=", 1], ["image", "!=", ""]],
    order_by="idx",
)
```

`filters` accepts Frappe's list-of-conditions form (`[[field, operator,
value], ...]`), not just a flat dict of equalities — `list_data_script()`
validates each condition the same way regardless of shape. Bind the image
with `binding("image", "src")` (auto-detected as `type="attribute"`, same
as any other `src`/`href`-shaped binding — TRAP-003 rule 4) inside its own
repeater, separate from the record's normal category listing. An item that
satisfies the filter appears in *both* places — the featured strip and its
ordinary category — which is usually what's wanted, not a bug to guard
against.

## Maintaining it

Once the doctype and page exist, day-to-day changes need none of the above:

1. Log in to the desk (`/login`, then the doctype's list view —
   `/app/<doctype-name-slugified>`, e.g. `/app/menu-item`).
2. **Add**, **edit**, or **delete** records there, same as any other Frappe
   list — "+ Add \<Doctype\>", fill the fields, save.
3. Refresh the published page. No rebuild, no `buildsmith load`, no touching
   the Builder page at all.

A field meant to be filled in only sometimes (an image tied to a "featured"
flag, say) can carry `"depends_on": "is_featured"` in its field definition
— the desk form then only shows it once the flag is checked, which keeps
the common case (an ordinary, unfeatured record) uncluttered.

The one thing that *isn't* just data: a brand-new **category** that has no
matching static heading on the page has nowhere to render — the categories
are literal blocks in the page's design, not derived from the data. Adding a
new section is a structural change (step 2–4 above, once), not a content
edit.

## Known traps and limits

- **TRAP-020 — always `frappe.db.get_all`, never `frappe.db.get_list` or
  `frappe.get_list`/`frappe.get_all`.** Builder's `page_data_script` runs in
  its own sandbox, distinct from a Server Script's. The bare `frappe.get_*`
  functions don't exist in it at all (loud, forgiving `AttributeError`).
  `frappe.db.get_list` exists but is permission-checked as whoever is
  viewing the page — it works every time you preview it as yourself and
  500s for a real, logged-out visitor. `frappe.db.get_all` always ignores
  permissions by design — the right default for content meant to be public.
  `list_data_script()` enforces this; there is no parameter to opt into
  `.get_list`. See `docs/traps.md` for the full writeup, including the one
  caveat: this guarantee is conditional on the target bench's Server
  Scripts setting.
- **TRAP-003 — repeater rules.** `isRepeaterBlock`, a single child, and a
  `dataKey` are required together and fail silently if any one is missing;
  attribute-shaped bindings (`src`, `href`, …) need `type="attribute"` or
  the binding vanishes with no error. `primitives.repeater.repeater()` and
  `binding()` enforce all of this — build repeaters with them, not by hand.
- **No cross-category grouping in one repeater.** Each heading + item-list
  pair is its own repeater against its own filtered query. Cheap at the
  scale this pattern is for (a menu, a roster); not the shape to reach for
  if a page needs thousands of records grouped many ways.
- **Field names can't contain `(`.** Builder's sandbox silently drops such
  a field from the query rather than erroring. `list_data_script()` refuses
  it at build time instead.

## Where the code lives

All of it is public, in this repository, already on `main`:

| what | where |
|---|---|
| `Page.page_data_script` field | `buildsmith/primitives/template.py` |
| `list_data_script()`, `DataPageError` | `buildsmith/primitives/datapage.py` |
| `repeater()`, `binding()` (pre-existing, TRAP-003) | `buildsmith/primitives/repeater.py` |
| `design/pages/*.json` → `page_data_script` wiring | `buildsmith/workflows/theme/build.py` |
| the automated proof (TRAP-020, live-sandbox check) | `buildsmith/tools/check_traps.py` |
| the trap writeup | `docs/traps.md` |
| tests | `tests/test_datapage.py`, `tests/test_template.py` |

**What is *not* here:** the actual doctype records and the composed page for
any one site. Those are that site's own data — `sites/<site>/design/` and
whatever records exist on its target Frappe instance — and `sites/<site>/`
is gitignored (only `sites/example/` is committed). This document describes
the reusable capability; a specific site's menu, roster, or price list is
built on top of it, in that site's own private layer, never in this repo's
tracked history.
