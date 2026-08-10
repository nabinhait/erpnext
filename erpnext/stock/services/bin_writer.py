# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

"""Single write path for `tabBin`.

Every statement that creates, mutates, or deletes Bin rows belongs here, so
the table has exactly one write address. Business logic (computing the
quantities) stays with the callers; this module owns the writes.
"""

from typing import TYPE_CHECKING

import frappe

if TYPE_CHECKING:
	from frappe.model.document import Document


def create(item_code: str, warehouse: str) -> "Document":
	"""Create a Bin, tolerating a concurrent insert of the same key — the only insert path."""
	savepoint = "create_bin"
	try:
		frappe.db.savepoint(savepoint)
		bin_obj = frappe.get_doc(doctype="Bin", item_code=item_code, warehouse=warehouse)
		bin_obj.flags.ignore_permissions = 1
		bin_obj.insert()
	except frappe.UniqueValidationError:
		frappe.db.rollback(save_point=savepoint)  # preserve transaction in postgres
		bin_obj = frappe.get_last_doc("Bin", {"item_code": item_code, "warehouse": warehouse})

	return bin_obj


def set_fields(bin_: "Document | str", values: dict, update_modified: bool = True) -> None:
	"""Update fields on one Bin row; ``bin_`` is a Document or a name."""
	if isinstance(bin_, str):
		frappe.db.set_value("Bin", bin_, values, update_modified=update_modified)
	else:
		bin_.db_set(values, update_modified=update_modified)


def update_document(bin_doc: "Document") -> None:
	"""Write a loaded Bin document back as a full-row UPDATE (no hooks)."""
	bin_doc.db_update()


def save(bin_doc: "Document") -> None:
	"""Full document save with hooks — repair tooling (Recalculate Values)."""
	bin_doc.save()


def set_stock_uom_for_item(item_code: str, stock_uom: str) -> None:
	"""Sync every Bin of an item after its stock UOM changed (only legal with no ledger)."""
	table = frappe.qb.DocType("Bin")
	frappe.qb.update(table).set(table.stock_uom, stock_uom).where(table.item_code == item_code).run()


def delete(filters: dict) -> None:
	"""Delete Bin rows (item/warehouse deletion, item merge, company wipe)."""
	frappe.db.delete("Bin", filters)
