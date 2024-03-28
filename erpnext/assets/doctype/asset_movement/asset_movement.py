# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe import _
from frappe.model.document import Document
from pypika import Order

from erpnext.assets.doctype.asset_activity.asset_activity import add_asset_activity


class AssetMovement(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.assets.doctype.asset_movement_item.asset_movement_item import AssetMovementItem

		amended_from: DF.Link | None
		assets: DF.Table[AssetMovementItem]
		company: DF.Link
		purpose: DF.Literal["", "Issue", "Receipt", "Transfer"]
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		transaction_date: DF.Datetime
	# end: auto-generated types

	def validate(self):
		self.validate_asset()
		self.validate_location()
		self.validate_employee()

	def validate_asset(self):
		for d in self.assets:
			status, company = frappe.db.get_value("Asset", d.asset, ["status", "company"])
			if self.purpose == "Transfer" and status in ("Draft", "Scrapped", "Sold"):
				frappe.throw(_("{0} asset cannot be transferred").format(status))

			if company != self.company:
				frappe.throw(_("Asset {0} does not belong to company {1}").format(d.asset, self.company))

	def validate_location(self):
		for d in self.assets:
			if not (d.source_location or d.target_location or d.from_employee or d.to_employee):
				frappe.throw(_("Either location or employee must be required"))

			if self.purpose == "Transfer" and d.source_location == d.target_location:
				frappe.throw(_("Source and Target Location cannot be same"))

	def validate_employee(self):
		for d in self.assets:
			if d.from_employee:
				current_custodian = frappe.db.get_value("Asset", d.asset, "custodian")

				if current_custodian != d.from_employee:
					frappe.throw(
						_("Asset {0} does not belongs to the custodian {1}").format(d.asset, d.from_employee)
					)

			if d.to_employee and frappe.db.get_value("Employee", d.to_employee, "company") != self.company:
				frappe.throw(
					_("Employee {0} does not belongs to the company {1}").format(d.to_employee, self.company)
				)

	def on_submit(self):
		self.set_latest_location_in_asset()
		self.set_custodian_in_asset()
		self.log_asset_activity()

	def on_cancel(self):
		self.set_latest_location_in_asset()
		self.set_custodian_in_asset()
		self.log_asset_activity()

	def set_latest_location_in_asset(self):
		current_location = None
		for d in self.assets:
			latest_transfer = self.get_asset_movement(d.asset, "Transfer")
			if latest_transfer:
				current_location = latest_transfer[0].get("target_location")
			else:
				# get location from first cancelled movement
				first_cancel_mov = self.get_asset_movement(d.asset, "Transfer", docstatus=2, order_type="asc")
				if first_cancel_mov:
					current_location = first_cancel_mov[0].get("source_location")

			frappe.db.set_value("Asset", d.asset, "location", current_location, update_modified=False)

	def set_custodian_in_asset(self):
		current_custodian = None
		for d in self.assets:
			latest_issue = self.get_asset_movement(d.asset, "Issue")
			if latest_issue:
				current_custodian = latest_issue[0].get("to_employee")

			frappe.db.set_value("Asset", d.asset, "custodian", current_custodian, update_modified=False)

	def get_asset_movement(self, asset, purpose, docstatus=1, order_type="desc"):
		asc_desc = Order.desc if order_type == "desc" else Order.asc
		mov = frappe.qb.DocType("Asset Movement")
		mov_item = frappe.qb.DocType("Asset Movement Item")
		asset_movement = (
			frappe.qb.from_(mov)
			.inner_join(mov_item)
			.on(mov.name == mov_item.parent)
			.select(mov_item.source_location, mov_item.target_location, mov_item.to_employee, mov.purpose)
			.where(mov_item.asset == asset)
			.where(mov.company == self.company)
			.where(mov.purpose == purpose)
			.where(mov.docstatus == docstatus)
			.orderby(mov.transaction_date, order=asc_desc)
			.limit(1)
		).run(as_dict=True)

		return asset_movement

	def log_asset_activity(self):
		for d in self.assets:
			if self.docstatus == 1:
				if self.purpose == "Transfer":
					message = _("Asset transferred from {0} to {1}").format(d.source_location, d.target_location)
				elif self.purpose == "Issue":
					message = _("Asset issued to Employee {0}").format(d.to_employee)
				elif self.purpose == "Receipt":
					message = _("Asset received from Employee {0}").format(d.from_employee)
			else:
				message = _("Asset Movement {0} cancelled").format(self.name)

			add_asset_activity(d.asset, message)
