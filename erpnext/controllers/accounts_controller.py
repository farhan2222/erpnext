# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe, erpnext
import json
import re
from frappe import _, throw, scrub
from frappe.utils import today, flt, cint, fmt_money, formatdate, getdate, add_days, add_months, get_last_day, nowdate,\
	cstr
from erpnext.setup.utils import get_exchange_rate
from erpnext.accounts.utils import get_fiscal_years, validate_fiscal_year, get_account_currency
from erpnext.utilities.transaction_base import TransactionBase
from erpnext.buying.utils import update_last_purchase_rate
from erpnext.controllers.sales_and_purchase_return import validate_return
from erpnext.accounts.party import get_party_account_currency, validate_party_frozen_disabled
from erpnext.exceptions import InvalidCurrency
from six import text_type

force_item_fields = ("item_group", "brand", "stock_uom", "is_fixed_asset", "item_tax_rate", "allow_zero_valuation_rate",
	"apply_discount_after_taxes")


class AccountsController(TransactionBase):
	def __init__(self, *args, **kwargs):
		super(AccountsController, self).__init__(*args, **kwargs)

	@property
	def company_currency(self):
		if not hasattr(self, "__company_currency"):
			self.__company_currency = erpnext.get_company_currency(self.company)

		return self.__company_currency

	def onload(self):
		self.set_onload("make_payment_via_journal_entry",
			frappe.db.get_single_value('Accounts Settings', 'make_payment_via_journal_entry'))

		if self.is_new():
			relevant_docs = ("Quotation", "Purchase Order", "Sales Order",
							 "Purchase Invoice", "Sales Invoice")
			if self.doctype in relevant_docs:
				self.set_payment_schedule()

	def ensure_supplier_is_not_blocked(self):
		supplier, supplier_name = None, None
		is_buying_invoice, is_supplier_payment = False, False

		if self.doctype == 'Payment Entry' and self.party_type == 'Supplier':
			supplier_name = self.party
			is_supplier_payment = True
			supplier = frappe.get_doc('Supplier', supplier_name)
		elif self.doctype in ['Purchase Invoice', 'Purchase Order']:
			supplier_name = self.supplier
			is_buying_invoice = True
		elif self.doctype == 'Landed Cost Voucher' and self.party_type == 'Supplier':
			supplier_name = self.party
			is_buying_invoice = True

		if supplier_name:
			supplier = frappe.get_doc('Supplier', supplier_name)

		if supplier and supplier.on_hold:
			if (is_buying_invoice and supplier.hold_type in ['All', 'Invoices']) or \
					(is_supplier_payment and supplier.hold_type in ['All', 'Payments']):
				if not supplier.release_date or getdate(nowdate()) <= supplier.release_date:
					frappe.msgprint(
						_('{0} is blocked so this transaction cannot proceed'.format(supplier_name)), raise_exception=1)

	def validate(self):

		if not self.get('is_return'):
			self.validate_qty_is_not_zero()

		if self.get("_action") and self._action != "update_after_submit":
			self.set_missing_values(for_validate=True)

		self.ensure_supplier_is_not_blocked()

		self.validate_date_with_fiscal_year()

		if self.meta.get_field("currency"):
			self.calculate_taxes_and_totals()

			if not self.meta.get_field("is_return") or not self.is_return:
				self.validate_value("base_grand_total", ">=", 0)

			validate_return(self)

			if self.meta.get_field("in_words") or self.meta.get_field("base_in_words"):
				self.set_total_in_words()

		self.validate_all_documents_schedule()

		if self.meta.get_field("taxes_and_charges"):
			self.validate_enabled_taxes_and_charges()
			self.validate_tax_account_company()

		self.validate_party()
		self.validate_currency()

		self.clean_remarks()

		if self.doctype == 'Purchase Invoice':
			self.calculate_paid_amount()

		if self.doctype in ['Purchase Invoice', 'Sales Invoice']:
			pos_check_field = "is_pos" if self.doctype=="Sales Invoice" else "is_paid"
			if cint(self.allocate_advances_automatically) and not cint(self.get(pos_check_field)):
				self.set_advances()
		elif self.doctype in ['Landed Cost Voucher'] and cint(self.allocate_advances_automatically):
			self.set_advances()

		if self.doctype in ['Purchase Invoice', 'Sales Invoice'] and self.is_return:
				self.validate_qty()

		validate_regional(self)

	def validate_invoice_documents_schedule(self):
		self.validate_payment_schedule_dates()
		self.set_due_date()
		self.set_payment_schedule()
		self.validate_payment_schedule_amount()
		self.validate_due_date()
		self.validate_advance_entries()

	def validate_non_invoice_documents_schedule(self):
		self.set_payment_schedule()
		self.validate_payment_schedule_dates()
		self.validate_payment_schedule_amount()

	def validate_all_documents_schedule(self):
		if self.doctype in ("Sales Invoice", "Purchase Invoice") and not self.is_return:
			self.validate_invoice_documents_schedule()
		elif self.doctype in ("Quotation", "Purchase Order", "Sales Order"):
			self.validate_non_invoice_documents_schedule()

	def before_print(self):
		if self.doctype in ['Purchase Order', 'Sales Order', 'Sales Invoice', 'Purchase Invoice',
							'Supplier Quotation', 'Purchase Receipt', 'Delivery Note', 'Quotation']:
			if self.get("group_same_items"):
				self.group_similar_items()

			self.warehouses = list(set([frappe.get_cached_value("Warehouse", item.warehouse, 'warehouse_name')
				for item in self.items if item.get('warehouse')]))

			for item in self.items:
				item.alt_uom_or_uom = item.alt_uom or item.uom

			if self.get("discount_amount"):
				self.discount_amount = -self.discount_amount

			if self.get("total_discount_after_taxes"):
				self.total_discount_after_taxes = -self.total_discount_after_taxes

			df = self.meta.get_field("discount_amount")
			if self.get("discount_amount") and hasattr(self, "taxes") and not len(self.taxes):
				df.set("print_hide", 0)
			else:
				df.set("print_hide", 1)

		if self.doctype in ['Journal Entry', 'Payment Entry']:
			self.get_gl_entries_for_print()

		self.company_address_doc = erpnext.get_company_address(self)

		if self.doctype == "Stock Entry":
			self.s_warehouses = list(set([frappe.get_cached_value("Warehouse", item.s_warehouse, 'warehouse_name')
				for item in self.items if item.get('s_warehouse')]))
			self.t_warehouses = list(set([frappe.get_cached_value("Warehouse", item.t_warehouse, 'warehouse_name')
				for item in self.items if item.get('t_warehouse')]))

	def calculate_paid_amount(self):
		if hasattr(self, "is_pos") or hasattr(self, "is_paid"):
			is_paid = self.get("is_pos") or self.get("is_paid")

			if is_paid:
				if not self.cash_bank_account:
					# show message that the amount is not paid
					frappe.throw(_("Note: Payment Entry will not be created since 'Cash or Bank Account' was not specified"))

				if cint(self.is_return) and (self.grand_total > self.paid_amount):
					self.paid_amount = flt(flt(self.grand_total), self.precision("paid_amount"))

				elif not flt(self.paid_amount) and flt(self.outstanding_amount) > 0:
					self.paid_amount = flt(flt(self.outstanding_amount), self.precision("paid_amount"))

				self.base_paid_amount = flt(self.paid_amount * self.conversion_rate,
										self.precision("base_paid_amount"))

	def set_missing_values(self, for_validate=False):
		if frappe.flags.in_test:
			for fieldname in ["posting_date", "transaction_date"]:
				if self.meta.get_field(fieldname) and not self.get(fieldname):
					self.set(fieldname, today())
					break

	def calculate_taxes_and_totals(self):
		from erpnext.controllers.taxes_and_totals import calculate_taxes_and_totals
		calculate_taxes_and_totals(self)

		if self.doctype in ["Quotation", "Sales Order", "Delivery Note", "Sales Invoice"]:
			self.calculate_commission()
			self.calculate_contribution()

	def validate_date_with_fiscal_year(self):
		if self.meta.get_field("fiscal_year"):
			date_field = ""
			if self.meta.get_field("posting_date"):
				date_field = "posting_date"
			elif self.meta.get_field("transaction_date"):
				date_field = "transaction_date"

			if date_field and self.get(date_field):
				validate_fiscal_year(self.get(date_field), self.fiscal_year, self.company,
									 self.meta.get_label(date_field), self)

	def validate_due_date(self):
		if self.get('is_pos'): return

		from erpnext.accounts.party import validate_due_date
		if self.doctype == "Sales Invoice":
			if not self.due_date:
				frappe.throw(_("Due Date is mandatory"))

			validate_due_date(self.posting_date, self.due_date,
				"Customer", self.customer, self.company, self.payment_terms_template)
		elif self.doctype == "Purchase Invoice":
			validate_due_date(self.bill_date or self.posting_date, self.due_date,
				"Supplier", self.supplier, self.company, self.bill_date, self.payment_terms_template)

	def set_price_list_currency(self, buying_or_selling):
		if self.meta.get_field("posting_date"):
			transaction_date = self.posting_date
		else:
			transaction_date = self.transaction_date

		if self.meta.get_field("currency"):
			# price list part
			if buying_or_selling.lower() == "selling":
				fieldname = "selling_price_list"
				args = "for_selling"
			else:
				fieldname = "buying_price_list"
				args = "for_buying"

			if self.meta.get_field(fieldname) and self.get(fieldname):
				self.price_list_currency = frappe.db.get_value("Price List",
															   self.get(fieldname), "currency")

				if self.price_list_currency == self.company_currency:
					self.plc_conversion_rate = 1.0

				elif not self.plc_conversion_rate:
					self.plc_conversion_rate = get_exchange_rate(self.price_list_currency,
																 self.company_currency, transaction_date, args)

			# currency
			if not self.currency:
				self.currency = self.price_list_currency
				self.conversion_rate = self.plc_conversion_rate
			elif self.currency == self.company_currency:
				self.conversion_rate = 1.0
			elif not self.conversion_rate:
				self.conversion_rate = get_exchange_rate(self.currency,
														 self.company_currency, transaction_date, args)

	def set_missing_item_details(self, for_validate=False):
		"""set missing item values"""
		from erpnext.stock.get_item_details import get_item_details
		from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos

		if hasattr(self, "items"):
			parent_dict = {}
			for fieldname in self.meta.get_valid_columns():
				parent_dict[fieldname] = self.get(fieldname)

			if self.doctype in ["Quotation", "Sales Order", "Delivery Note", "Sales Invoice"]:
				document_type = "{} Item".format(self.doctype)
				parent_dict.update({"document_type": document_type})

			if 'transaction_type' in parent_dict:
				parent_dict['transaction_type_name'] = parent_dict.pop('transaction_type')

			# party_name field used for customer in quotation
			if self.doctype == "Quotation" and self.quotation_to == "Customer" and parent_dict.get("party_name"):
				parent_dict.update({"customer": parent_dict.get("party_name")})

			for item in self.get("items"):
				if item.get("item_code"):
					args = parent_dict.copy()
					args.update(item.as_dict())

					args["doctype"] = self.doctype
					args["name"] = self.name

					if not args.get("transaction_date"):
						args["transaction_date"] = args.get("posting_date")

					if self.get("is_subcontracted"):
						args["is_subcontracted"] = self.is_subcontracted
					ret = get_item_details(args)

					for fieldname, value in ret.items():
						if item.meta.get_field(fieldname) and value is not None:
							if (item.get(fieldname) is None or fieldname in force_item_fields):
								item.set(fieldname, value)

							elif fieldname in ['cost_center', 'conversion_factor'] and not item.get(fieldname):
								item.set(fieldname, value)

							elif fieldname == "serial_no":
								# Ensure that serial numbers are matched against Stock UOM
								item_conversion_factor = item.get("conversion_factor") or 1.0
								item_qty = abs(item.get("qty")) * item_conversion_factor

								if item_qty != len(get_serial_nos(item.get('serial_no'))):
									item.set(fieldname, value)

					if self.doctype in ["Purchase Invoice", "Sales Invoice"] and item.meta.get_field('is_fixed_asset'):
						item.set('is_fixed_asset', ret.get('is_fixed_asset', 0))

					if ret.get("pricing_rule"):
						# if user changed the discount percentage then set user's discount percentage ?
						item.set("pricing_rule", ret.get("pricing_rule"))
						item.set("discount_percentage", ret.get("discount_percentage"))
						if ret.get("pricing_rule_for") == "Rate":
							item.set("price_list_rate", ret.get("price_list_rate"))

						if item.price_list_rate:
							item.rate = flt(item.price_list_rate *
											(1.0 - (flt(item.discount_percentage) / 100.0)), item.precision("rate"))

			if self.doctype == "Purchase Invoice":
				self.set_expense_account(for_validate)

	def set_taxes(self):
		if not self.meta.get_field("taxes"):
			return

		tax_master_doctype = self.meta.get_field("taxes_and_charges").options

		if (self.is_new() or self.is_pos_profile_changed()) and not self.get("taxes"):
			if self.company and not self.get("taxes_and_charges"):
				# get the default tax master
				self.taxes_and_charges = frappe.db.get_value(tax_master_doctype,
															 {"is_default": 1, 'company': self.company})

			self.append_taxes_from_master(tax_master_doctype)

	def is_pos_profile_changed(self):
		if (self.doctype == 'Sales Invoice' and self.is_pos and
				self.pos_profile != frappe.db.get_value('Sales Invoice', self.name, 'pos_profile')):
			return True

	def append_taxes_from_master(self, tax_master_doctype=None):
		if self.get("taxes_and_charges"):
			if not tax_master_doctype:
				tax_master_doctype = self.meta.get_field("taxes_and_charges").options

			self.extend("taxes", get_taxes_and_charges(tax_master_doctype, self.get("taxes_and_charges")))

	def set_other_charges(self):
		self.set("taxes", [])
		self.set_taxes()

	def validate_enabled_taxes_and_charges(self):
		taxes_and_charges_doctype = self.meta.get_options("taxes_and_charges")
		if frappe.db.get_value(taxes_and_charges_doctype, self.taxes_and_charges, "disabled"):
			frappe.throw(_("{0} '{1}' is disabled").format(taxes_and_charges_doctype, self.taxes_and_charges))

	def validate_tax_account_company(self):
		for d in self.get("taxes"):
			if d.account_head:
				tax_account_company = frappe.db.get_value("Account", d.account_head, "company")
				if tax_account_company != self.company:
					frappe.throw(_("Row #{0}: Account {1} does not belong to company {2}")
								 .format(d.idx, d.account_head, self.company))

	def get_gl_dict(self, args, account_currency=None):
		"""this method populates the common properties of a gl entry record"""

		posting_date = args.get('posting_date') or self.get('posting_date')
		fiscal_years = get_fiscal_years(posting_date, company=self.company)
		if len(fiscal_years) > 1:
			frappe.throw(_("Multiple fiscal years exist for the date {0}. Please set company in Fiscal Year").format(
				formatdate(posting_date)))
		else:
			fiscal_year = fiscal_years[0][0]

		gl_dict = frappe._dict({
			'company': self.company,
			'posting_date': posting_date,
			'fiscal_year': fiscal_year,
			'voucher_type': self.doctype,
			'voucher_no': self.name,
			'remarks': self.get("remarks") or self.get("remark"),
			'debit': 0,
			'credit': 0,
			'debit_in_account_currency': 0,
			'credit_in_account_currency': 0,
			'is_opening': self.get("is_opening") or "No",
			'party_type': None,
			'party': None,
			'project': self.get("project") or self.get("set_project"),
			'cost_center': self.get("cost_center"),
			'reference_no': self.get("reference_no") or self.get("cheque_no") or self.get("bill_no"),
			'reference_date': self.get("reference_date") or self.get("cheque_date") or self.get("bill_date")
		})
		gl_dict.update(args)

		if not account_currency:
			account_currency = get_account_currency(gl_dict.account)

		if gl_dict.account and self.doctype not in ["Journal Entry",
													"Period Closing Voucher", "Payment Entry"]:
			self.validate_account_currency(gl_dict.account, account_currency)
			set_balance_in_account_currency(gl_dict, account_currency, self.get("conversion_rate"),
											self.company_currency)

		return gl_dict

	def validate_qty_is_not_zero(self):
		for item in self.items:
			if not item.qty and not item.get('rejected_qty'):
				frappe.throw("Item Quantity can not be zero")

	def validate_account_currency(self, account, account_currency=None):
		valid_currency = [self.company_currency]
		if self.get("currency") and self.currency != self.company_currency:
			valid_currency.append(self.currency)

		if account_currency not in valid_currency:
			frappe.throw(_("Account {0} is invalid. Account Currency must be {1}")
						 .format(account, _(" or ").join(valid_currency)))

	def clear_unallocated_advances(self, childtype, parentfield):
		self.set(parentfield, self.get(parentfield, {"allocated_amount": ["not in", [0, None, ""]]}))

		frappe.db.sql("""delete from `tab%s` where parentfield=%s and parent = %s
			and allocated_amount = 0""" % (childtype, '%s', '%s'), (parentfield, self.name))

	def apply_shipping_rule(self):
		if self.shipping_rule:
			shipping_rule = frappe.get_doc("Shipping Rule", self.shipping_rule)
			shipping_rule.apply(self)
			self.calculate_taxes_and_totals()

	def get_shipping_address(self):
		'''Returns Address object from shipping address fields if present'''

		# shipping address fields can be `shipping_address_name` or `shipping_address`
		# try getting value from both

		for fieldname in ('shipping_address_name', 'shipping_address'):
			shipping_field = self.meta.get_field(fieldname)
			if shipping_field and shipping_field.fieldtype == 'Link':
				if self.get(fieldname):
					return frappe.get_doc('Address', self.get(fieldname))

		return {}

	def set_advances(self):
		"""Returns list of advances against Account, Party, Reference"""

		res = self.get_advance_entries()
		company_currency = erpnext.get_company_currency(self.company)

		self.set("advances", [])
		advance_allocated = 0
		for d in res:
			if d.against_order:
				allocated_amount = flt(d.amount)
			else:
				if self.get("party_account_currency")\
					and self.get("party_account_currency") == company_currency:
					amount = self.get("base_rounded_total") or self.get("base_grand_total")
				else:
					amount = self.get("rounded_total") or self.get("grand_total")

				allocated_amount = min(flt(amount) - advance_allocated, d.amount)
			advance_allocated += flt(allocated_amount)

			self.append("advances", {
				"doctype": self.doctype + " Advance",
				"reference_type": d.reference_type,
				"reference_name": d.reference_name,
				"reference_row": d.reference_row,
				"remarks": d.remarks,
				"advance_amount": flt(d.amount),
				"allocated_amount": allocated_amount
			})

	def get_advance_entries(self, include_unallocated=True):
		against_all_orders = False
		order_field = None
		order_doctype = None
		if self.doctype == "Sales Invoice":
			party_account = self.debit_to
			party_type = "Customer"
			party = self.customer
			order_field = "sales_order"
			order_doctype = "Sales Order"
		elif self.doctype == "Purchase Invoice":
			party_account = self.credit_to
			party_type = "Letter of Credit" if self.letter_of_credit else "Supplier"
			party = self.letter_of_credit if self.letter_of_credit else self.supplier
			order_field = "purchase_order"
			order_doctype = "Purchase Order"
		elif self.doctype == "Expense Claim":
			party_account = self.payable_account
			party_type = "Employee"
			party = self.employee
		else:
			party_account = self.credit_to
			party_type = self.party_type
			party = self.party

		if order_field:
			order_list = list(set([d.get(order_field) for d in self.get("items") if d.get(order_field)]))
		else:
			order_list = []

		journal_entries = get_advance_journal_entries(party_type, party, party_account,
			order_doctype, order_list, include_unallocated, against_all_orders=against_all_orders)

		payment_entries = get_advance_payment_entries(party_type, party, party_account,
			order_doctype, order_list, include_unallocated, against_all_orders=against_all_orders)

		res = sorted(journal_entries + payment_entries, key=lambda d: (not bool(d.against_order), d.posting_date))

		return res

	def is_inclusive_tax(self):
		is_inclusive = cint(frappe.db.get_single_value("Accounts Settings",
													   "show_inclusive_tax_in_print"))

		if is_inclusive:
			is_inclusive = 0
			if self.get("taxes", filters={"included_in_print_rate": 1}):
				is_inclusive = 1

		return is_inclusive

	def validate_advance_entries(self):
		order_field = "sales_order" if self.doctype == "Sales Invoice" else "purchase_order"
		order_list = list(set([d.get(order_field)
							   for d in self.get("items") if d.get(order_field)]))

		if not order_list: return

		advance_entries = self.get_advance_entries(include_unallocated=False)

		if advance_entries:
			advance_entries_against_si = [d.reference_name for d in self.get("advances")]
			for d in advance_entries:
				if not advance_entries_against_si or d.reference_name not in advance_entries_against_si:
					frappe.msgprint(_(
						"Payment Entry {0} is linked against Order {1}, check if it should be pulled as advance in this invoice.")
									.format(d.reference_name, d.against_order))

	def update_against_document_in_jv(self):
		"""
			Links invoice and advance voucher:
				1. cancel advance voucher
				2. split into multiple rows if partially adjusted, assign against voucher
				3. submit advance voucher
		"""

		if self.doctype == "Sales Invoice":
			party_type = "Customer"
			party = self.customer
			party_account = self.debit_to
			dr_or_cr = "credit_in_account_currency"
		elif self.doctype == "Purchase Invoice":
			party_type = "Letter of Credit" if self.letter_of_credit else "Supplier"
			party = self.letter_of_credit if self.letter_of_credit else self.supplier
			party_account = self.credit_to
			dr_or_cr = "debit_in_account_currency"
		elif self.doctype == "Expense Claim":
			party_type = "Employee"
			party = self.employee
			party_account = self.payable_account
			dr_or_cr = "debit_in_account_currency"
		else:
			party_type = self.party_type
			party = self.party
			party_account = self.credit_to
			dr_or_cr = "debit_in_account_currency"

		if self.doctype in ["Sales Invoice", "Purchase Invoice"]:
			invoice_amounts = {
				'exchange_rate': (self.conversion_rate if self.party_account_currency != self.company_currency else 1),
				'grand_total': (self.base_grand_total if self.party_account_currency == self.company_currency else self.grand_total)
			}
		elif self.doctype == "Expense Claim":
			invoice_amounts = {
				'exchange_rate': 1,
				'grand_total': self.total_sanctioned_amount
			}
		else:
			invoice_amounts = {
				'exchange_rate': 1,
				'grand_total': self.grand_total
			}

		lst = []
		for d in self.get('advances'):
			if flt(d.allocated_amount) > 0 and d.reference_type != 'Employee Advance':
				args = frappe._dict({
					'voucher_type': d.reference_type,
					'voucher_no': d.reference_name,
					'voucher_detail_no': d.reference_row,
					'against_voucher_type': self.doctype,
					'against_voucher': self.name,
					'account': party_account,
					'party_type': party_type,
					'party': party,
					'dr_or_cr': dr_or_cr,
					'unadjusted_amount': flt(d.advance_amount),
					'allocated_amount': flt(d.allocated_amount),
					'outstanding_amount': self.outstanding_amount
				})
				args.update(invoice_amounts)
				lst.append(args)

		if lst:
			from erpnext.accounts.utils import reconcile_against_document
			reconcile_against_document(lst)

	def validate_multiple_billing(self, ref_dt, item_ref_dn, based_on, parentfield):
		from erpnext.controllers.status_updater import get_tolerance_for
		item_tolerance = {}
		global_tolerance = None

		for item in self.get("items"):
			if item.get(item_ref_dn):
				ref_amt = flt(frappe.db.get_value(ref_dt + " Item",
												  item.get(item_ref_dn), based_on), self.precision(based_on, item))
				if not ref_amt:
					frappe.msgprint(
						_("Warning: System will not check overbilling since amount for Item {0} in {1} is zero").format(
							item.item_code, ref_dt))
				else:
					already_billed = frappe.db.sql("""select sum(%s) from `tab%s`
						where %s=%s and docstatus=1 and parent != %s""" %
												   (based_on, self.doctype + " Item", item_ref_dn, '%s', '%s'),
												   (item.get(item_ref_dn), self.name))[0][0]

					total_billed_amt = flt(flt(already_billed) + flt(item.get(based_on)),
										   self.precision(based_on, item))

					tolerance, item_tolerance, global_tolerance = get_tolerance_for(item.item_code,
																					item_tolerance, global_tolerance)

					max_allowed_amt = flt(ref_amt * (100 + tolerance) / 100)

					if total_billed_amt - max_allowed_amt > 0.01:
						frappe.throw(_(
							"Cannot overbill for Item {0} in row {1} more than {2}. To allow over-billing, please set in Stock Settings").format(
							item.item_code, item.idx, max_allowed_amt))

	def get_company_default(self, fieldname):
		from erpnext.accounts.utils import get_company_default
		return get_company_default(self.company, fieldname)

	def get_stock_items(self):
		stock_items = []
		item_codes = list(set(item.item_code for item in self.get("items")))
		if item_codes:
			stock_items = [r[0] for r in frappe.db.sql("""select name
				from `tabItem` where name in (%s) and is_stock_item=1""" % \
													   (", ".join((["%s"] * len(item_codes))),), item_codes)]

		return stock_items

	def set_total_advance_paid(self):
		if self.doctype == "Sales Order":
			dr_or_cr = "credit_in_account_currency"
			party = self.customer
		else:
			dr_or_cr = "debit_in_account_currency"
			party = self.supplier

		advance = frappe.db.sql("""
			select
				account_currency, sum({dr_or_cr}) as amount
			from
				`tabGL Entry`
			where
				against_voucher_type = %s and against_voucher = %s and party=%s
				and docstatus = 1
		""".format(dr_or_cr=dr_or_cr), (self.doctype, self.name, party), as_dict=1)

		if advance:
			advance = advance[0]
			advance_paid = flt(advance.amount, self.precision("advance_paid"))
			formatted_advance_paid = fmt_money(advance_paid, precision=self.precision("advance_paid"),
											   currency=advance.account_currency)

			frappe.db.set_value(self.doctype, self.name, "party_account_currency",
								advance.account_currency)

			if advance.account_currency == self.currency:
				order_total = self.get("rounded_total") or self.grand_total
				precision = "rounded_total" if self.get("rounded_total") else "grand_total"
			else:
				order_total = self.get("base_rounded_total") or self.base_grand_total
				precision = "base_rounded_total" if self.get("base_rounded_total") else "base_grand_total"

			formatted_order_total = fmt_money(order_total, precision=self.precision(precision),
											  currency=advance.account_currency)

			if self.currency == self.company_currency and advance_paid > order_total:
				frappe.throw(_("Total advance ({0}) against Order {1} cannot be greater than the Grand Total ({2})")
							 .format(formatted_advance_paid, self.name, formatted_order_total))

			frappe.db.set_value(self.doctype, self.name, "advance_paid", advance_paid)

	@property
	def company_abbr(self):
		if not hasattr(self, "_abbr"):
			self._abbr = frappe.db.get_value('Company',  self.company,  "abbr")

		return self._abbr

	def validate_party(self):
		party_type, party = self.get_party()
		validate_party_frozen_disabled(party_type, party)

		billing_party_type, billing_party = self.get_billing_party()
		if (billing_party_type, billing_party) != (party_type, party):
			validate_party_frozen_disabled(billing_party_type, billing_party)

	def get_party(self):
		if self.doctype == "Landed Cost Voucher":
			return self.get("party_type"), self.get("party")

		party_type = None
		if self.doctype in ("Opportunity", "Quotation", "Sales Order", "Delivery Note", "Sales Invoice"):
			party_type = 'Customer'

		elif self.doctype in ("Supplier Quotation", "Purchase Order", "Purchase Receipt", "Purchase Invoice"):
			party_type = 'Supplier'

		elif self.meta.get_field("customer"):
			party_type = "Customer"

		elif self.meta.get_field("supplier"):
			party_type = "Supplier"

		party = self.get(scrub(party_type)) if party_type else None

		return party_type, party

	def get_billing_party(self):
		if self.get("letter_of_credit"):
			return "Letter of Credit", self.get("letter_of_credit")
		else:
			return self.get_party()

	def validate_currency(self):
		if self.get("currency"):
			party_type, party = self.get_billing_party()
			if party_type and party:
				party_account_currency = get_party_account_currency(party_type, party, self.company)

				if (party_account_currency
						and party_account_currency != self.company_currency
						and self.currency != party_account_currency):
					frappe.throw(_("Accounting Entry for {0}: {1} can only be made in currency: {2}")
								 .format(party_type, party, party_account_currency), InvalidCurrency)

				# Note: not validating with gle account because we don't have the account
				# at quotation / sales order level and we shouldn't stop someone
				# from creating a sales invoice if sales order is already created

	def clean_remarks(self):
		for f in ['remarks', 'remark', 'user_remark', 'user_remarks']:
			if self.meta.has_field(f):
				cleaned_remarks = cstr(self.get(f)).strip()
				cleaned_remarks = re.sub(r'\n\s*\n', '\n', cleaned_remarks)
				cleaned_remarks = re.sub(r' +', ' ', cleaned_remarks)
				self.set(f, cleaned_remarks)

	def validate_fixed_asset(self):
		for d in self.get("items"):
			if d.is_fixed_asset:
				# if d.qty > 1:
				# 					frappe.throw(_("Row #{0}: Qty must be 1, as item is a fixed asset. Please use separate row for multiple qty.").format(d.idx))

				if d.meta.get_field("asset") and d.asset:
					asset = frappe.get_doc("Asset", d.asset)

					if asset.company != self.company:
						frappe.throw(_("Row #{0}: Asset {1} does not belong to company {2}")
									 .format(d.idx, d.asset, self.company))

					elif asset.item_code != d.item_code:
						frappe.throw(_("Row #{0}: Asset {1} does not linked to Item {2}")
									 .format(d.idx, d.asset, d.item_code))

					# elif asset.docstatus != 1:
					# 						frappe.throw(_("Row #{0}: Asset {1} must be submitted").format(d.idx, d.asset))

					elif self.doctype == "Purchase Invoice":
						# if asset.status != "Submitted":
						# 							frappe.throw(_("Row #{0}: Asset {1} is already {2}")
						# 								.format(d.idx, d.asset, asset.status))
						if getdate(asset.purchase_date) != getdate(self.posting_date):
							frappe.throw(
								_("Row #{0}: Posting Date must be same as purchase date {1} of asset {2}").format(d.idx,
																												  asset.purchase_date,
																												  d.asset))
						elif asset.is_existing_asset:
							frappe.throw(
								_("Row #{0}: Purchase Invoice cannot be made against an existing asset {1}").format(
									d.idx, d.asset))

					elif self.docstatus == "Sales Invoice" and self.docstatus == 1:
						if self.update_stock:
							frappe.throw(_("'Update Stock' cannot be checked for fixed asset sale"))

						elif asset.status in ("Scrapped", "Cancelled", "Sold"):
							frappe.throw(_("Row #{0}: Asset {1} cannot be submitted, it is already {2}")
										 .format(d.idx, d.asset, asset.status))

	def delink_advance_entries(self, linked_doc_name):
		total_allocated_amount = 0
		for adv in self.advances:
			consider_for_total_advance = True
			if adv.reference_name == linked_doc_name:
				frappe.db.sql("""delete from `tab{0} Advance`
					where name = %s""".format(self.doctype), adv.name)
				consider_for_total_advance = False

			if consider_for_total_advance:
				total_allocated_amount += flt(adv.allocated_amount, adv.precision("allocated_amount"))

		frappe.db.set_value(self.doctype, self.name, "total_advance",
							total_allocated_amount, update_modified=False)

	def group_similar_items(self):
		group_item_data = {}
		item_meta = frappe.get_meta(self.doctype + " Item")
		count = 0

		sum_fields = ['qty', 'stock_qty', 'alt_uom_qty', 'total_weight',
			'amount', 'taxable_amount', 'net_amount', 'total_discount', 'amount_before_discount',
			'item_taxes_and_charges', 'tax_inclusive_amount']
		sum_fields += ['tax_exclusive_' + f for f in sum_fields if item_meta.has_field('tax_exclusive_' + f)]

		rate_fields = [('rate', 'amount'), ('taxable_rate', 'taxable_amount'), ('net_rate', 'net_amount'),
			('discount_amount', 'total_discount'), ('price_list_rate', 'amount_before_discount'),
			('tax_inclusive_rate', 'tax_inclusive_amount'), ('weight_per_unit', 'total_weight')]
		rate_fields += [('tax_exclusive_' + t, 'tax_exclusive_' + s) for t, s in rate_fields
			if item_meta.has_field('tax_exclusive_' + t) and item_meta.has_field('tax_exclusive_' + s)]

		base_fields = [('base_' + f, f) for f in sum_fields if item_meta.has_field('base_' + f)]
		base_fields += [('base_' + t, t) for t, s in rate_fields if item_meta.has_field('base_' + t)]

		# Sum amounts
		for item in self.items:
			group_key = (cstr(item.item_code), cstr(item.item_name), item.uom)
			group_item = group_item_data.setdefault(group_key, frappe._dict())
			for f in sum_fields:
				group_item[f] = group_item.get(f, 0) + flt(item.get(f))

			group_item_serial_nos = group_item.setdefault('serial_no', [])
			if item.get('serial_no'):
				group_item_serial_nos += filter(lambda s: s, item.serial_no.split('\n'))

		# Calculate average rates and get serial nos string
		for group_item in group_item_data.values():
			if group_item.qty:
				for target, source in rate_fields:
					group_item[target] = flt(group_item[source]) / flt(group_item.qty)
			else:
				for target, source in rate_fields:
					group_item[target] = 0

			group_item.serial_no = '\n'.join(group_item.serial_no)

		# Calculate company currenct values
		for group_item in group_item_data.values():
			for target, source in base_fields:
				group_item[target] = group_item.get(source, 0) * self.conversion_rate

		# Remove duplicates and set aggregated values
		duplicate_list = []
		for item in self.items:
			group_key = (cstr(item.item_code), cstr(item.item_name), item.uom)
			if group_key in group_item_data.keys():
				count += 1

				# Will set price_list_rate instead
				if item.get('rate_with_margin'):
					item.rate_with_margin = 0
				if item.get('tax_exclusive_rate_with_margin'):
					item.tax_exclusive_rate_with_margin = 0

				item.update(group_item_data[group_key])

				item.idx = count
				del group_item_data[group_key]
			else:
				duplicate_list.append(item)

		for item in duplicate_list:
			self.remove(item)

	def get_gl_entries_for_print(self):
		from collections import OrderedDict

		if self.docstatus == 1:
			gles = frappe.db.sql("""
				select
					account, remarks, party_type, party, debit, credit,
					against_voucher, against_voucher_type, reference_no, reference_date
				from `tabGL Entry`
				where voucher_type = %s and voucher_no = %s
			""", [self.doctype, self.name], as_dict=1)
		else:
			gles = self.get_gl_entries()

		grouped_gles = OrderedDict()

		for gle in gles:
			key = (gle.account, cstr(gle.party_type), cstr(gle.party), cstr(gle.remarks), cstr(gle.reference_no),
				cstr(gle.reference_date), bool(gle.against_voucher))
			group = grouped_gles.setdefault(key, frappe._dict({
				"account": cstr(gle.account),
				"party_type": cstr(gle.party_type),
				"party": cstr(gle.party),
				"remarks": cstr(gle.remarks),
				"reference_no": cstr(gle.reference_no),
				"reference_date": cstr(gle.reference_date),
				"sum": 0, "against_voucher_set": set(), "against_voucher": []
			}))
			group.sum += flt(gle.debit) - flt(gle.credit)
			if gle.against_voucher_type and gle.against_voucher:
				group.against_voucher_set.add((cstr(gle.against_voucher_type), cstr(gle.against_voucher)))

		for d in grouped_gles.values():
			d.debit = d.sum if d.sum > 0 else 0
			d.credit = -d.sum if d.sum < 0 else 0

			for against_voucher_type, against_voucher in d.against_voucher_set:
				bill_no = None
				if against_voucher_type in ['Journal Entry', 'Purchase Invoice']:
					bill_no = frappe.db.get_value(against_voucher_type, against_voucher, 'bill_no')

				if bill_no:
					d.against_voucher.append(bill_no)
				else:
					d.against_voucher.append(frappe.utils.get_original_name(against_voucher_type, against_voucher))

			d.against_voucher = ", ".join(d.against_voucher or [])

		debit_gles = filter(lambda d: d.debit - d.credit > 0, grouped_gles.values())
		credit_gles = filter(lambda d: d.debit - d.credit < 0, grouped_gles.values())

		self.gl_entries = debit_gles + credit_gles
		self.total_debit = sum([d.debit for d in self.gl_entries])
		self.total_credit = sum([d.credit for d in self.gl_entries])

	def set_payment_schedule(self):
		if self.doctype == 'Sales Invoice' and self.is_pos:
			self.payment_terms_template = ''
			return

		posting_date = self.get("bill_date") or self.get("posting_date") or self.get("transaction_date")
		date = self.get("due_date")
		due_date = date or posting_date
		grand_total = self.get("rounded_total") or self.grand_total
		if self.doctype in ("Sales Invoice", "Purchase Invoice"):
			grand_total = grand_total - flt(self.write_off_amount)

		if self.get("total_advance"):
			grand_total -= self.get("total_advance")

		if not self.get("payment_schedule"):
			if self.get("payment_terms_template"):
				data = get_payment_terms(self.payment_terms_template, posting_date, grand_total)
				for item in data:
					self.append("payment_schedule", item)
			else:
				data = dict(due_date=due_date, invoice_portion=100, payment_amount=grand_total)
				self.append("payment_schedule", data)
		else:
			for d in self.get("payment_schedule"):
				if d.invoice_portion:
					d.payment_amount = grand_total * flt(d.invoice_portion) / 100

	def set_due_date(self):
		due_dates = [d.due_date for d in self.get("payment_schedule") if d.due_date]
		if due_dates:
			self.due_date = max(due_dates)

	def validate_payment_schedule_dates(self):
		dates = []
		li = []

		if self.doctype == 'Sales Invoice' and self.is_pos: return

		for d in self.get("payment_schedule"):
			if self.doctype == "Sales Order" and getdate(d.due_date) < getdate(self.transaction_date):
				frappe.throw(_("Row {0}: Due Date cannot be before posting date").format(d.idx))
			elif d.due_date in dates:
				li.append(_("{0} in row {1}").format(d.due_date, d.idx))
			dates.append(d.due_date)

		if li:
			duplicates = '<br>' + '<br>'.join(li)
			frappe.throw(_("Rows with duplicate due dates in other rows were found: {0}").format(duplicates))

	def validate_payment_schedule_amount(self):
		if self.doctype == 'Sales Invoice' and self.is_pos: return

		if self.get("payment_schedule"):
			total = 0
			for d in self.get("payment_schedule"):
				total += flt(d.payment_amount)
			total = flt(total, self.precision("grand_total"))

			grand_total = flt(self.get("rounded_total") or self.grand_total)
			if self.get("total_advance"):
				grand_total -= self.get("total_advance")

			if self.doctype in ("Sales Invoice", "Purchase Invoice"):
				grand_total = grand_total - flt(self.write_off_amount)
			grand_total = flt(grand_total, self.precision('grand_total'))

			if total != grand_total:
				frappe.throw(_("Total Payment Amount in Payment Schedule must be equal to Grand / Rounded Total"))

	def is_rounded_total_disabled(self):
		if self.meta.get_field("calculate_tax_on_company_currency") and cint(self.get("calculate_tax_on_company_currency")):
			return True
		if self.meta.get_field("disable_rounded_total"):
			return self.disable_rounded_total
		else:
			return frappe.db.get_single_value("Global Defaults", "disable_rounded_total")

@frappe.whitelist()
def get_tax_rate(account_head):
	return frappe.db.get_value("Account", account_head, ["tax_rate", "account_name"], as_dict=True)


@frappe.whitelist()
def get_default_taxes_and_charges(master_doctype, tax_template=None, company=None):
	if not company: return {}

	if tax_template and company:
		tax_template_company = frappe.db.get_value(master_doctype, tax_template, "company")
		if tax_template_company == company:
			return

	default_tax = frappe.db.get_value(master_doctype, {"is_default": 1, "company": company})

	return {
		'taxes_and_charges': default_tax,
		'taxes': get_taxes_and_charges(master_doctype, default_tax)
	}


@frappe.whitelist()
def get_taxes_and_charges(master_doctype, master_name):
	if not master_name:
		return
	from frappe.model import default_fields
	tax_master = frappe.get_doc(master_doctype, master_name)

	taxes_and_charges = []
	for i, tax in enumerate(tax_master.get("taxes")):
		tax = tax.as_dict()

		for fieldname in default_fields:
			if fieldname in tax:
				del tax[fieldname]

		taxes_and_charges.append(tax)

	return taxes_and_charges


def validate_conversion_rate(currency, conversion_rate, conversion_rate_label, company):
	"""common validation for currency and price list currency"""

	company_currency = frappe.get_cached_value('Company',  company,  "default_currency")

	if not conversion_rate:
		throw(_("{0} is mandatory. Maybe Currency Exchange record is not created for {1} to {2}.").format(
			conversion_rate_label, currency, company_currency))


def validate_taxes_and_charges(tax):
	if tax.charge_type in ['Actual', 'On Net Total'] and tax.row_id:
		frappe.throw(_("Can refer row only if the charge type is 'On Previous Row Amount' or 'Previous Row Total'"))
	elif tax.charge_type in ['On Previous Row Amount', 'On Previous Row Total']:
		if cint(tax.idx) == 1:
			frappe.throw(
				_("Cannot select charge type as 'On Previous Row Amount' or 'On Previous Row Total' for first row"))
		elif not tax.row_id:
			frappe.throw(_("Please specify a valid Row ID for row {0} in table {1}".format(tax.idx, _(tax.doctype))))
		elif tax.row_id and cint(tax.row_id) >= cint(tax.idx):
			frappe.throw(_("Cannot refer row number greater than or equal to current row number for this Charge type"))

	if tax.charge_type == "Actual":
		tax.rate = None


def validate_inclusive_tax(tax, doc):
	def _on_previous_row_error(row_range):
		throw(_("To include tax in row {0} in Item rate, taxes in rows {1} must also be included").format(tax.idx,
																										  row_range))

	if cint(getattr(tax, "included_in_print_rate", None)):
		if tax.charge_type == "Actual":
			# inclusive tax cannot be of type Actual
			throw(_("Charge of type 'Actual' in row {0} cannot be included in Item Rate").format(tax.idx))
		elif tax.charge_type == "Weighted Distribution":
			# inclusive tax cannot be of type Actual
			throw(_("Charge of type 'Weighted Distribution' in row {0} cannot be included in Item Rate").format(tax.idx))
		elif tax.charge_type == "On Previous Row Amount" and \
				not cint(doc.get("taxes")[cint(tax.row_id) - 1].included_in_print_rate):
			# referred row should also be inclusive
			_on_previous_row_error(tax.row_id)
		elif tax.charge_type == "On Previous Row Total" and \
				not all([cint(t.included_in_print_rate) for t in doc.get("taxes")[:cint(tax.row_id) - 1]]):
			# all rows about the reffered tax should be inclusive
			_on_previous_row_error("1 - %d" % (tax.row_id,))
		elif tax.get("category") == "Valuation":
			frappe.throw(_("Valuation type charges can not marked as Inclusive"))


def set_balance_in_account_currency(gl_dict, account_currency=None, conversion_rate=None, company_currency=None):
	if (not conversion_rate) and (account_currency != company_currency):
		frappe.throw(_("Account: {0} with currency: {1} can not be selected")
					 .format(gl_dict.account, account_currency))

	gl_dict["account_currency"] = company_currency if account_currency == company_currency \
		else account_currency

	# set debit/credit in account currency if not provided
	if flt(gl_dict.debit) and not flt(gl_dict.debit_in_account_currency):
		gl_dict.debit_in_account_currency = gl_dict.debit if account_currency == company_currency \
			else flt(gl_dict.debit / conversion_rate, 2)

	if flt(gl_dict.credit) and not flt(gl_dict.credit_in_account_currency):
		gl_dict.credit_in_account_currency = gl_dict.credit if account_currency == company_currency \
			else flt(gl_dict.credit / conversion_rate, 2)


def get_advance_journal_entries(party_type, party, party_account, order_doctype,
		order_list=None, include_unallocated=True, against_all_orders=False, against_account=None, limit=None):
	journal_entries = []
	if erpnext.get_party_account_type(party_type) == "Receivable":
		dr_or_cr = "credit_in_account_currency"
		bal_dr_or_cr = "gle_je.credit_in_account_currency - gle_je.debit_in_account_currency"
		payment_dr_or_cr = "gle_payment.debit_in_account_currency - gle_payment.credit_in_account_currency"
	else:
		dr_or_cr = "debit_in_account_currency"
		bal_dr_or_cr = "gle_je.debit_in_account_currency - gle_je.credit_in_account_currency"
		payment_dr_or_cr = "gle_payment.credit_in_account_currency - gle_payment.debit_in_account_currency"

	limit_cond = "limit %(limit)s" if limit else ""

	# JVs against order documents
	if order_list or against_all_orders:
		if order_list:
			order_condition = "and ifnull(jea.reference_name, '') in ('{0}')" \
				.format("', '".join([frappe.db.escape(d) for d in order_list]))
		else:
			order_condition = "and ifnull(jea.reference_name, '') != ''"

		against_account_condition = "and jea.against_account like '%%{0}%%'".format(frappe.db.escape(against_account)) \
			if against_account else ""

		journal_entries += frappe.db.sql("""
			select
				"Journal Entry" as reference_type, je.name as reference_name, je.remark as remarks,
				jea.{dr_or_cr} as amount, jea.name as reference_row, jea.reference_name as against_order,
				je.posting_date
			from
				`tabJournal Entry` je, `tabJournal Entry Account` jea
			where
				je.name = jea.parent and jea.account = %(account)s
				and jea.party_type = %(party_type)s and jea.party = %(party)s
				and {dr_or_cr} > 0 and jea.reference_type = '{order_doctype}' and je.docstatus = 1
				{order_condition} {against_account_condition}
			order by je.posting_date
			{limit_cond}""".format(
				dr_or_cr=dr_or_cr,
				order_doctype=order_doctype,
				order_condition=order_condition,
				against_account_condition=against_account_condition,
				limit_cond=limit_cond
			), {
			"party_type": party_type,
			"party": party,
			"account": party_account,
			"limit": limit
			}, as_dict=1)

	# Unallocated payment JVs
	if include_unallocated:
		against_account_condition = ""
		if against_account:
			against_account_condition = "and GROUP_CONCAT(gle_je.against) like '%%{0}%%'".format(frappe.db.escape(against_account))

		journal_entries += frappe.db.sql("""
		select
			gle_je.voucher_type as reference_type, je.name as reference_name, je.remark as remarks, je.posting_date,
			ifnull(sum({bal_dr_or_cr}), 0) - (
				select ifnull(sum({payment_dr_or_cr}), 0)
				from `tabGL Entry` gle_payment
				where
					gle_payment.against_voucher_type = gle_je.voucher_type
					and gle_payment.against_voucher = gle_je.voucher_no
					and gle_payment.party_type = gle_je.party_type
					and gle_payment.party = gle_je.party
					and gle_payment.account = gle_je.account
					and abs({payment_dr_or_cr}) > 0
			) as amount
		from `tabGL Entry` gle_je
		inner join `tabJournal Entry` je on je.name = gle_je.voucher_no
		where
			gle_je.party_type = %(party_type)s and gle_je.party = %(party)s and gle_je.account = %(account)s
			and gle_je.voucher_type = 'Journal Entry' and (gle_je.against_voucher = '' or gle_je.against_voucher is null)
			and abs({bal_dr_or_cr}) > 0
		group by gle_je.voucher_no
		having amount > 0.005 {against_account_condition}
		order by gle_je.posting_date
		{limit_cond}""".format(
			bal_dr_or_cr=bal_dr_or_cr,
			payment_dr_or_cr=payment_dr_or_cr,
			against_account_condition=against_account_condition,
			limit_cond=limit_cond
		), {
			"party_type": party_type,
			"party": party,
			"account": party_account,
			"limit": limit
		}, as_dict=True)

	return list(journal_entries)


def get_advance_payment_entries(party_type, party, party_account, order_doctype,
		order_list=None, include_unallocated=True, against_all_orders=False, against_account=None, limit=None):
	payment_entries_against_order, unallocated_payment_entries = [], []
	party_account_type = erpnext.get_party_account_type(party_type)
	party_account_field = "paid_from" if party_account_type == "Receivable" else "paid_to"
	against_account_field = "paid_to" if party_account_type == "Receivable" else "paid_from"
	payment_type = "Receive" if party_account_type == "Receivable" else "Pay"
	limit_cond = "limit %s" % limit if limit else ""

	against_account_condition = ""
	if against_account:
		against_account_condition = "and pe.{against_account_field} = {against_account}".format(
			against_account_field=against_account_field, against_account=frappe.db.escape(against_account))

	if order_list or against_all_orders:
		if order_list:
			reference_condition = " and pref.reference_name in ({0})" \
				.format(', '.join(['%s'] * len(order_list)))
		else:
			reference_condition = ""
			order_list = []

		payment_entries_against_order = frappe.db.sql("""
			select
				"Payment Entry" as reference_type, pe.name as reference_name,
				pe.remarks, pref.allocated_amount as amount, pref.name as reference_row,
				pref.reference_name as against_order, pe.posting_date
			from `tabPayment Entry` pe, `tabPayment Entry Reference` pref
			where
				pe.name = pref.parent and pe.{party_account_field} = %s and pe.payment_type = %s
				and pe.party_type = %s and pe.party = %s and pe.docstatus = 1
				and pref.reference_doctype = %s
				{reference_condition} {against_account_condition}
			order by pe.posting_date
			{limit_cond}
		""".format(
			party_account_field=party_account_field,
			reference_condition=reference_condition,
			against_account_condition=against_account_condition,
			limit_cond=limit_cond
		), [party_account, payment_type, party_type, party, order_doctype] + order_list, as_dict=1)

	if include_unallocated:
		unallocated_payment_entries = frappe.db.sql("""
			select "Payment Entry" as reference_type, name as reference_name, remarks, unallocated_amount as amount,
				pe.posting_date
			from `tabPayment Entry` pe
			where
				{party_account_field} = %s and party_type = %s and party = %s and payment_type = %s
				and docstatus = 1 and unallocated_amount > 0
				{against_account_condition}
			order by posting_date
			{limit_cond}
		""".format(
			party_account_field=party_account_field,
			against_account_condition=against_account_condition,
			limit_cond=limit_cond
		), [party_account, party_type, party, payment_type], as_dict=1)

	return list(payment_entries_against_order) + list(unallocated_payment_entries)


def update_invoice_status():
	# Daily update the status of the invoices

	frappe.db.sql(""" update `tabSales Invoice` set status = 'Overdue'
		where due_date < CURDATE() and docstatus = 1 and outstanding_amount > 0""")

	frappe.db.sql(""" update `tabPurchase Invoice` set status = 'Overdue'
		where due_date < CURDATE() and docstatus = 1 and outstanding_amount > 0""")


@frappe.whitelist()
def get_payment_terms(terms_template, posting_date=None, grand_total=None, bill_date=None):
	if not terms_template:
		return

	terms_doc = frappe.get_doc("Payment Terms Template", terms_template)

	schedule = []
	for d in terms_doc.get("terms"):
		term_details = get_payment_term_details(d, posting_date, grand_total, bill_date)
		schedule.append(term_details)

	return schedule


@frappe.whitelist()
def get_payment_term_details(term, posting_date=None, grand_total=None, bill_date=None):
	term_details = frappe._dict()
	if isinstance(term, text_type):
		term = frappe.get_doc("Payment Term", term)
	else:
		term_details.payment_term = term.payment_term
	term_details.description = term.description
	term_details.invoice_portion = term.invoice_portion
	term_details.payment_amount = flt(term.invoice_portion) * flt(grand_total) / 100
	if bill_date:
		term_details.due_date = get_due_date(term, bill_date)
	elif posting_date:
		term_details.due_date = get_due_date(term, posting_date)

	if getdate(term_details.due_date) < getdate(posting_date):
		term_details.due_date = posting_date
	term_details.mode_of_payment = term.mode_of_payment

	return term_details


def get_due_date(term, posting_date=None, bill_date=None):
	due_date = None
	date = bill_date or posting_date
	if term.due_date_based_on == "Day(s) after invoice date":
		due_date = add_days(date, term.credit_days)
	elif term.due_date_based_on == "Day(s) after the end of the invoice month":
		due_date = add_days(get_last_day(date), term.credit_days)
	elif term.due_date_based_on == "Month(s) after the end of the invoice month":
		due_date = add_months(get_last_day(date), term.credit_months)
	return due_date


def get_supplier_block_status(party_name):
	"""
	Returns a dict containing the values of `on_hold`, `release_date` and `hold_type` of
	a `Supplier`
	"""
	supplier = frappe.get_doc('Supplier', party_name)
	info = {
		'on_hold': supplier.on_hold,
		'release_date': supplier.release_date,
		'hold_type': supplier.hold_type
	}
	return info


@frappe.whitelist()
def update_child_qty_rate(parent_doctype, trans_items, parent_doctype_name):
	data = json.loads(trans_items)
	for d in data:
		child_item = frappe.get_doc(parent_doctype + ' Item', d.get("docname"))

		if parent_doctype == "Sales Order" and flt(d.get("qty")) < child_item.delivered_qty:
			frappe.throw(_("Row #{0}: Cannot set quantity less than delivered quantity").format(child_item.idx))

		if parent_doctype == "Purchase Order" and flt(d.get("qty")) < child_item.received_qty:
			frappe.throw(_("Row #{0}: Cannot set quantity less than received quantity").format(child_item.idx))

		if flt(child_item.get("qty")) < flt(child_item.get("billed_qty")):
			frappe.throw(_("Row #{0}: Cannot set quantity less than billed quantity").format(child_item.idx))

		child_item.qty = flt(d.get("qty"))

		if flt(child_item.get("billed_amt")) > (flt(d.get("rate")) * flt(d.get("qty"))):
			frappe.throw(_("Row #{0}: Cannot set Rate if amount is greater than billed amount for Item {1}.")
						 .format(child_item.idx, child_item.item_code))
		else:
			child_item.rate = flt(d.get("rate"))
		if flt(child_item.price_list_rate):
			if flt(child_item.rate) > flt(child_item.price_list_rate):
				#  if rate is greater than price_list_rate, set margin
				#  or set discount
				child_item.discount_percentage = 0
				child_item.margin_type = "Amount"
				child_item.margin_rate_or_amount = flt(child_item.rate - child_item.price_list_rate,
					child_item.precision("margin_rate_or_amount"))
				child_item.rate_with_margin = child_item.rate
			else:
				child_item.discount_percentage = flt((1 - flt(child_item.rate) / flt(child_item.price_list_rate)) * 100.0,
					child_item.precision("discount_percentage"))
				child_item.discount_amount = flt(
					child_item.price_list_rate) - flt(child_item.rate)
				child_item.margin_type = ""
				child_item.margin_rate_or_amount = 0
				child_item.rate_with_margin = 0

		child_item.flags.ignore_validate_update_after_submit = True
		child_item.save()

	p_doctype = frappe.get_doc(parent_doctype, parent_doctype_name)

	p_doctype.flags.ignore_validate_update_after_submit = True
	p_doctype.set_qty_as_per_stock_uom()
	p_doctype.calculate_taxes_and_totals()
	frappe.get_doc('Authorization Control').validate_approving_authority(p_doctype.doctype,
		p_doctype.company, p_doctype.base_grand_total)

	p_doctype.set_payment_schedule()
	if parent_doctype == 'Purchase Order':
		p_doctype.validate_minimum_order_qty()
		p_doctype.validate_budget()
		if p_doctype.is_against_so():
			p_doctype.update_status_updater()
	else:
		p_doctype.check_credit_limit()

	p_doctype.save()

	if parent_doctype == 'Purchase Order':
		update_last_purchase_rate(p_doctype, is_submit = 1)
		p_doctype.update_prevdoc_status()
		p_doctype.update_requested_qty()
		p_doctype.update_ordered_qty()
		p_doctype.update_ordered_and_reserved_qty()
		p_doctype.update_receiving_percentage()
		if p_doctype.is_subcontracted == "Yes":
			p_doctype.update_reserved_qty_for_subcontract()
	else:
		p_doctype.update_reserved_qty()
		p_doctype.update_project()
		p_doctype.update_prevdoc_status('submit')
		p_doctype.update_delivery_status()

	p_doctype.update_blanket_order()
	p_doctype.update_billing_percentage()
	p_doctype.set_status()

@erpnext.allow_regional
def validate_regional(doc):
	pass
