// Copyright (c) 2016, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on("Asset Movement", {
	setup: (frm) => {
		frm.set_query("to_employee", "assets", (doc) => {
			return {
				filters: {
					company: doc.company,
				},
			};
		});

		frm.set_query("from_employee", "assets", (doc) => {
			return {
				filters: {
					company: doc.company,
				},
			};
		});

		frm.set_query("reference_name", (doc) => {
			return {
				filters: {
					company: doc.company,
					docstatus: 1,
				},
			};
		});

		frm.set_query("reference_doctype", () => {
			return {
				filters: {
					name: ["in", ["Purchase Receipt", "Purchase Invoice"]],
				},
			};
		}),
			frm.set_query("asset", "assets", () => {
				return {
					filters: {
						status: ["not in", ["Draft"]],
					},
				};
			});
	},
});
