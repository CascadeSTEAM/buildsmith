"""Load an exported Builder site into the local dev instance, exactly.

Runs INSIDE the bench container. Reads a JSON job from stdin, writes a JSON
report to stdout prefixed with `ADOPT:`.

Why this runs through bench rather than the REST API, when everything else is
moving the other way: **`frappe.flags.in_import`**. Frappe's `set_new_name()`
does

    if autoname.lower() not in ("prompt", "uuid") and not frappe.flags.in_import:
        doc.name = None

so a supplied `name` is discarded on every ordinary insert, including every REST
insert. There is no whitelisted method to set the flag, and there should not be.

That one flag is the difference between a copy and a lookalike:

- **Builder Variable names are UUIDs, and block styles reference them as
  `var(--<uuid>)`.** Let Frappe mint new ones and every reference in every block
  points at a variable that does not exist. The page then renders on its literal
  fallbacks and looks *almost* right — which is exactly what ADR-005 recorded as
  "tokens do not round-trip". They round-trip fine; the flag was missing.
- **Page names become choosable**, so a re-import updates the page it replaces
  instead of minting a second one racing for the same route (TRAP-012, BS-010).
- It also suppresses `BuilderComponent.on_update`'s `queue_action`, which locks
  the document and enqueues a job (TRAP-009, TRAP-017).

Order matters and is not negotiable: variables, then components, then pages.
A page referencing a component that does not exist yet fails validation, and a
component styled with a variable that does not exist yet renders on fallbacks.
"""

import json
import sys

import frappe


def upsert(record, doctype, report):
    """Insert or update one record, preserving its name."""
    name = record.get("name")
    record["doctype"] = doctype
    # Server-managed columns. Carrying them over makes Frappe reject the write
    # or silently misattribute authorship, and neither is content.
    for field in ("owner", "modified_by", "creation", "modified", "idx",
                  "docstatus", "_user_tags", "_comments", "_assign", "_liked_by"):
        record.pop(field, None)

    if name and frappe.db.exists(doctype, name):
        doc = frappe.get_doc(doctype, name)
        doc.update(record)
        doc.save()
        report["updated"] += 1
    else:
        frappe.get_doc(record).insert()
        report["inserted"] += 1
    return name


def main():
    job = json.loads(sys.stdin.read())
    site = job["site"]
    report = {"site": site, "steps": [], "errors": []}

    frappe.init(site=site)
    frappe.connect()

    # After init, not before: `frappe.flags` is a thread-local proxy and is not
    # bound until then. Setting it at module scope raises "object is not bound".
    #
    # in_import is the flag this whole module exists for. Frappe's
    # set_new_name() discards a supplied `name` unless it is set, so without it
    # every Builder Variable is minted a fresh UUID and every var(--uuid) in
    # every block style points at a variable that does not exist.
    frappe.flags.in_import = True
    # No workers are assumed; queue_action would lock documents that never
    # unlock (TRAP-009, TRAP-017).
    frappe.flags.in_migrate = True

    for doctype, records in job["order"]:
        step = {"doctype": doctype, "inserted": 0, "updated": 0,
                "failed": 0, "names": [], "failures": []}
        for record in records:
            try:
                name = upsert(dict(record), doctype, step)
                if name:
                    step["names"].append(name)
            except Exception as exc:  # noqa: BLE001 — one bad record must not
                # abort the rest; a partial load reported honestly beats an
                # aborted load whose partial state nobody knows about.
                step["failed"] += 1
                step["failures"].append(
                    {"name": record.get("name"), "error": f"{type(exc).__name__}: {exc}"[:300]}
                )
        report["steps"].append(step)

    # Route collisions. An adopt makes dev match the export, so any OTHER
    # published page already serving one of the export's routes is a problem:
    # Builder resolves duplicates with `order_by published_at desc, creation
    # desc`, so the loser becomes silently unreachable with no error anywhere
    # (TRAP-010). Detected always; removed only when asked.
    adopted = {n for step in report["steps"] if step["doctype"] == "Builder Page"
               for n in step["names"]}
    routes = {r for r in (
        frappe.db.get_value("Builder Page", n, "route") for n in adopted) if r}
    collisions = []
    for route in sorted(routes):
        for other in frappe.get_all(
            "Builder Page",
            filters={"route": route, "published": 1, "name": ["not in", list(adopted) or [""]]},
            pluck="name",
        ):
            collisions.append({"route": route, "name": other})
            if job.get("prune"):
                frappe.delete_doc("Builder Page", other, force=True)
    report["collisions"] = collisions
    report["pruned"] = bool(job.get("prune"))

    if job.get("home_page"):
        frappe.db.set_value("Website Settings", "Website Settings",
                            "home_page", job["home_page"])
        report["home_page"] = job["home_page"]

    frappe.db.commit()

    # Prove the thing this module exists to guarantee, rather than assuming it:
    # every Builder Variable UUID referenced by a block style must resolve.
    existing = set(frappe.get_all("Builder Variable", pluck="name"))
    report["variables_present"] = len(existing)
    report["variables_expected"] = len(
        [r for dt, rs in job["order"] if dt == "Builder Variable" for r in rs]
    )

    frappe.destroy()
    print("ADOPT:" + json.dumps(report))


if __name__ == "__main__":
    main()
