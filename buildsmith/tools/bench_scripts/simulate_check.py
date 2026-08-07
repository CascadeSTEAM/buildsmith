import copy
import json

import frappe

frappe.init(site="sandbox.localhost"); frappe.connect()
from builder.builder.doctype.builder_page.builder_page import extend_block


def collapsed(component, page_shell):
    child = extend_block(copy.deepcopy(component), copy.deepcopy(page_shell))["children"][0]
    return child.get("element") is None and child.get("innerHTML") is None

out = []
for current, proposed, page_shell in json.load(open("/simulate-scenarios.json")):
    before = collapsed(current, page_shell)
    after = collapsed(proposed, page_shell)
    out.append(after and not before)   # caused by this payload
print("RESULT:" + json.dumps(out))
