"""Tests for `list_data_script` — the TRAP-020-safe page_data_script builder.

Pure Python: these check the generated script's *text* and this module's own
refusals. Whether Builder's sandbox actually treats a Guest visitor the way
TRAP-020 records is a different question, answered against the live pinned
sandbox by `check_traps.py` — the same split `test_traps.py`'s own docstring
draws between itself and `sandbox/trap-check.py`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith.primitives.datapage import DataPageError, list_data_script  # noqa: E402


class ListDataScript(unittest.TestCase):
    def test_emits_get_all_not_get_list(self):
        script = list_data_script(
            target="menu_items", doctype="Menu Item", fields=["item_name", "price"]
        )
        self.assertIn("frappe.db.get_all(", script)
        self.assertNotIn("get_list", script)
        self.assertTrue(script.startswith("data.menu_items = "))

    def test_the_script_is_valid_python_and_assigns_the_target(self):
        script = list_data_script(
            target="menu_items", doctype="Menu Item", fields=["item_name", "price"],
            filters={"is_available": 1}, order_by="item_name",
        )
        calls = []

        class FakeFrappeDb:
            def get_all(self, *args, **kwargs):
                calls.append((args, kwargs))
                return [{"item_name": "Toast"}]

        class FakeFrappe:
            db = FakeFrappeDb()

        class AttrDict(dict):
            # A stand-in for frappe._dict — attribute access over a dict,
            # which is what `data` actually is in Builder's own sandbox.
            __getattr__ = dict.get
            __setattr__ = dict.__setitem__

        data = AttrDict()
        exec(compile(script, "<test>", "exec"), {"frappe": FakeFrappe(), "data": data})
        self.assertEqual(data["menu_items"], [{"item_name": "Toast"}])
        args, kwargs = calls[0]
        self.assertEqual(args, ("Menu Item",))
        self.assertEqual(kwargs["fields"], ["item_name", "price"])
        self.assertEqual(kwargs["filters"], {"is_available": 1})
        self.assertEqual(kwargs["order_by"], "item_name")

    def test_no_limit_kwarg_is_forced_here(self):
        # get_all's own default (limit_page_length=0, i.e. unlimited, per
        # builder/utils.py's safe_get_all) is what we rely on — this module
        # must not override it with something smaller.
        script = list_data_script(target="items", doctype="Item", fields=["name"])
        self.assertNotIn("limit_page_length", script)
        self.assertNotIn("limit=", script)

    def test_bad_target_refused(self):
        with self.assertRaises(DataPageError):
            list_data_script(target="", doctype="Item", fields=["name"])
        with self.assertRaises(DataPageError):
            list_data_script(target="menu items", doctype="Item", fields=["name"])
        with self.assertRaises(DataPageError):
            list_data_script(target="class", doctype="Item", fields=["name"])

    def test_empty_doctype_refused(self):
        with self.assertRaises(DataPageError):
            list_data_script(target="items", doctype="", fields=["name"])

    def test_empty_fields_refused(self):
        with self.assertRaises(DataPageError):
            list_data_script(target="items", doctype="Item", fields=[])

    def test_field_with_parenthesis_refused_loudly(self):
        # Builder's own sandbox drops this silently (remove_unsafe_fields) —
        # this module refuses it instead of shipping a script that quietly
        # returns fewer columns than it asked for.
        with self.assertRaises(DataPageError) as caught:
            list_data_script(target="items", doctype="Item", fields=["COUNT(name)"])
        self.assertIn("(", str(caught.exception))

    def test_unsafe_filter_value_refused(self):
        with self.assertRaises(DataPageError):
            list_data_script(
                target="items", doctype="Item", fields=["name"],
                filters={"owner": object()},
            )

    def test_non_finite_float_refused(self):
        # repr(float("nan")) is the bare text "nan" — a name, not a literal.
        # It would compile, then raise NameError when the script actually
        # runs, which is exactly the "looks fine until someone loads the
        # page" shape TRAP-020 is about.
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(DataPageError):
                list_data_script(
                    target="items", doctype="Item", fields=["name"],
                    filters={"price": bad},
                )

    def test_list_shaped_filters_are_supported(self):
        # Frappe's other filter syntax: [[field, operator, value], ...].
        script = list_data_script(
            target="items", doctype="Item", fields=["name"],
            filters=[["price", ">=", 5], ["is_available", "=", 1]],
        )
        calls = []

        class FakeFrappeDb:
            def get_all(self, *args, **kwargs):
                calls.append(kwargs)
                return []

        class FakeFrappe:
            db = FakeFrappeDb()

        class AttrDict(dict):
            __getattr__ = dict.get
            __setattr__ = dict.__setitem__

        exec(compile(script, "<test>", "exec"), {"frappe": FakeFrappe(), "data": AttrDict()})
        self.assertEqual(calls[0]["filters"], [["price", ">=", 5], ["is_available", "=", 1]])


if __name__ == "__main__":
    unittest.main()
