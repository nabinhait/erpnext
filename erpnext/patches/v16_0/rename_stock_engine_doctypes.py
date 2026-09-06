import frappe

RENAMED_DOCTYPES = (
	("Stock Fold State", "Stock Engine State"),
	("Stock Fold Checkpoint", "Stock Engine Snapshot"),
	("Stock Refold", "Stock Recompute Request"),
)
RENAMED_REPORTS = ("Stock Balance Fold", "Stock Ledger Fold", "Stock Ageing Fold")


def execute():
	"""The stock engine's vocabulary changed: fold state, checkpoints and refold
	requests are engine state, snapshots and recompute requests. Reports sync
	fresh under their new names."""
	for old, new in RENAMED_DOCTYPES:
		if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
			frappe.rename_doc("DocType", old, new, force=True)
	for report in RENAMED_REPORTS:
		frappe.delete_doc("Report", report, ignore_missing=True, force=True)
