# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# For license information, please see license.txt

from frappe.model.document import Document


class PricingSchemeMigration(Document):
	"""Assistant over the migration services: convert legacy Pricing
	Rules, then replay recent documents before flipping the engine.
	All logic lives in erpnext.accounts.services.pricing.pricing_migration.
	"""

	pass
