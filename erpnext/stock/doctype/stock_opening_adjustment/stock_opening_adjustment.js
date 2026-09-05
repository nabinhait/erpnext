// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
// License: GNU General Public License v3. See license.txt

frappe.ui.form.on("Stock Opening Adjustment", {
	refresh(frm) {
		if (frm.doc.docstatus !== 0 || frm.is_new()) {
			return;
		}
		if (["Draft", "Ready", "Failed"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Compute"), () => {
				frm.call({ method: "compute", doc: frm.doc, freeze: true, callback: () => frm.reload_doc() });
			});
		}
		if (frm.doc.status === "Ready" && !frm.doc.within_threshold) {
			frm.dashboard.set_headline(
				__("The value delta exceeds the review threshold. Review the keys below before submitting.")
			);
		}
	},
});
