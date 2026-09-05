// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
// For license information, please see license.txt

frappe.ui.form.on("Pricing Scheme Migration", {
	refresh(frm) {
		frm.disable_save();
		frm.trigger("render_status");
		frm.add_custom_button(__("Dry Run"), () => run_conversion(frm, 1));
		frm.add_custom_button(__("Run Replay"), () => run_replay(frm));
		frm.page.set_primary_action(__("Convert"), () => {
			frappe.confirm(
				__(
					"Create Pricing Schemes for every unconverted Pricing Rule? Risky conversions are inserted disabled."
				),
				() => run_conversion(frm, 0)
			);
		});
	},

	render_status(frm) {
		frappe.call({
			method: "erpnext.accounts.services.pricing.pricing_migration.get_migration_status",
			callback: ({ message: status }) => {
				// indicators append; empty the stats row so re-renders replace instead
				frm.dashboard.stats_area_row.empty();
				frm.dashboard.add_indicator(
					__("Engine: {0}", [status.engine]),
					status.engine === "Pricing Scheme" ? "green" : "gray"
				);
				frm.dashboard.add_indicator(
					__("Converted {0} of {1} rules", [status.converted, status.total_rules]),
					status.pending ? "orange" : "green"
				);
				if (status.disabled_conversions) {
					frm.dashboard.add_indicator(
						__("{0} conversions await review", [status.disabled_conversions]),
						"red"
					);
				}
			},
		});
	},
});

function run_conversion(frm, dry_run) {
	frappe.call({
		method: "erpnext.accounts.services.pricing.pricing_migration.convert_legacy_pricing_rules",
		args: { dry_run },
		freeze: true,
		freeze_message: dry_run ? __("Analyzing Pricing Rules...") : __("Converting Pricing Rules..."),
		callback: ({ message }) => {
			render_conversion_report(frm, message, dry_run);
			frm.trigger("render_status");
		},
	});
}

function render_conversion_report(frm, report, dry_run) {
	const colors = { clean: "green", "behavior change": "orange", "needs review": "red" };
	const rows = (report.converted || [])
		.map((entry) => {
			const schemes = (entry.schemes || [])
				.map((s) =>
					dry_run
						? frappe.utils.escape_html(s)
						: `<a href="/app/pricing-scheme/${s}">${frappe.utils.escape_html(s)}</a>`
				)
				.join(", ");
			return `<tr>
				<td><a href="/app/pricing-rule/${entry.rule}">${frappe.utils.escape_html(entry.rule)}</a></td>
				<td><span class="indicator ${colors[entry.classification] || "gray"}">${frappe.utils.escape_html(
				entry.classification
			)}</span></td>
				<td>${schemes}</td>
				<td class="small text-muted">${frappe.utils.escape_html((entry.notes || []).join("; "))}</td>
			</tr>`;
		})
		.join("");

	const summary = dry_run
		? __("Dry run: {0} rules would convert, {1} already converted. Composition would be set to {2}.", [
				(report.converted || []).length,
				(report.skipped || []).length,
				report.composition,
		  ])
		: __("Converted {0} rules, {1} were already converted. Composition set to {2}.", [
				(report.converted || []).length,
				(report.skipped || []).length,
				report.composition,
		  ]);

	frm.get_field("conversion_report").$wrapper.html(`
		<div style="font-weight: 600; margin-bottom: 6px;">${summary}</div>
		${
			rows
				? `<table class="table table-bordered table-sm">
					<thead><tr><th>${__("Pricing Rule")}</th><th>${__("Classification")}</th><th>${__("Schemes")}</th><th>${__(
						"Notes"
				  )}</th></tr></thead>
					<tbody>${rows}</tbody>
				</table>`
				: `<div class="text-muted">${__("Nothing left to convert.")}</div>`
		}
	`);
}

function run_replay(frm) {
	frappe.call({
		method: "erpnext.accounts.services.pricing.pricing_migration.replay_recent_documents",
		args: { days: frm.doc.replay_days || 90, limit: frm.doc.replay_limit || 100 },
		freeze: true,
		freeze_message: __("Repricing recent documents..."),
		callback: ({ message }) => render_replay_report(frm, message),
	});
}

function render_replay_report(frm, report) {
	const rows = (report.diffs || [])
		.map(
			(diff) => `<tr>
				<td><a href="/app/${frappe.router.slug(diff.voucher_type)}/${diff.voucher_no}">${frappe.utils.escape_html(
				diff.voucher_no
			)}</a></td>
				<td>${frappe.utils.escape_html(diff.item_code)}</td>
				<td class="text-right">${format_currency(diff.old_rate)}</td>
				<td class="text-right">${format_currency(diff.new_rate)}</td>
				<td class="text-right ${diff.delta < 0 ? "text-danger" : ""}">${format_currency(diff.delta)}</td>
			</tr>`
		)
		.join("");

	const clean = !report.lines_changed;
	const summary = clean
		? __("Checked {0} documents ({1} lines): the new engine reproduces every rate.", [
				report.documents_checked,
				report.lines_checked,
		  ])
		: __("Checked {0} documents ({1} lines): {2} lines would change, total delta {3}.", [
				report.documents_checked,
				report.lines_checked,
				report.lines_changed,
				format_currency(report.total_delta),
		  ]);

	const skipped = report.engine_priced_lines
		? `<div class="small text-muted" style="margin-bottom: 6px;">${__(
				"{0} lines were already priced by the new engine and were not compared.",
				[report.engine_priced_lines]
		  )}</div>`
		: "";

	frm.get_field("replay_report").$wrapper.html(`
		<div style="font-weight: 600; margin-bottom: 6px;" class="${clean ? "text-success" : ""}">${summary}</div>
		${skipped}
		${
			rows
				? `<table class="table table-bordered table-sm">
					<thead><tr><th>${__("Document")}</th><th>${__("Item")}</th><th class="text-right">${__(
						"Charged Rate"
				  )}</th><th class="text-right">${__("New Rate")}</th><th class="text-right">${__(
						"Delta"
				  )}</th></tr></thead>
					<tbody>${rows}</tbody>
				</table>
				<div class="small text-muted">${__("Largest 20 differences shown.")}</div>`
				: ""
		}
	`);
}
