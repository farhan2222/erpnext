// Copyright (c) 2016, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt
/* eslint-disable */

frappe.query_reports["Sales Details"] = {
	"filters": [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1
		},
		{
			fieldname: "doctype",
			label: __("Based On"),
			fieldtype: "Select",
			options: ["Sales Order","Delivery Note","Sales Invoice"],
			default: "Sales Invoice",
			reqd: 1
		},
		{
			fieldname: "order_type",
			label: __("Order Type"),
			fieldtype: "Select",
			options: "\nSales\nMaintenance\nShopping Cart"
		},
		{
			fieldname: "qty_field",
			label: __("Quantity Type"),
			fieldtype: "Select",
			options: ["Stock Qty", "Contents Qty", "Transaction Qty"],
			default: "Stock Qty",
			reqd: 1
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
			reqd: 1
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1
		},
		{
			fieldname: "customer",
			label: __("Customer"),
			fieldtype: "Link",
			options: "Customer"
		},
		{
			fieldname: "customer_group",
			label: __("Customer Group"),
			fieldtype: "Link",
			options: "Customer Group"
		},
		{
			fieldname: "item_code",
			label: __("Item"),
			fieldtype: "Link",
			options: "Item"
		},
		{
			fieldname: "item_group",
			label: __("Item Group"),
			fieldtype: "Link",
			options: "Item Group"
		},
		{
			fieldname: "brand",
			label: __("Brand"),
			fieldtype: "Link",
			options: "Brand"
		},
		{
			fieldname: "territory",
			label: __("Territory"),
			fieldtype: "Link",
			options: "Territory"
		},
		{
			fieldname: "sales_person",
			label: __("Sales Person"),
			fieldtype: "Link",
			options: "Sales Person"
		},
		{
			"fieldname":"cost_center",
			"label": __("Cost Center"),
			"fieldtype": "MultiSelect",
			get_data: function() {
				var cost_centers = frappe.query_report.get_filter_value("cost_center") || "";

				const values = cost_centers.split(/\s*,\s*/).filter(d => d);
				const txt = cost_centers.match(/[^,\s*]*$/)[0] || '';
				let data = [];

				frappe.call({
					type: "GET",
					method:'frappe.desk.search.search_link',
					async: false,
					no_spinner: true,
					args: {
						doctype: "Cost Center",
						txt: txt,
						filters: {
							"company": frappe.query_report.get_filter_value("company"),
							"name": ["not in", values]
						}
					},
					callback: function(r) {
						data = r.results;
					}
				});
				return data;
			}
		},
		{
			"fieldname":"project",
			"label": __("Project"),
			"fieldtype": "MultiSelect",
			get_data: function() {
				var projects = frappe.query_report.get_filter_value("project") || "";

				const values = projects.split(/\s*,\s*/).filter(d => d);
				const txt = projects.match(/[^,\s*]*$/)[0] || '';
				let data = [];

				frappe.call({
					type: "GET",
					method:'frappe.desk.search.search_link',
					async: false,
					no_spinner: true,
					args: {
						doctype: "Project",
						txt: txt,
						filters: {
							"name": ["not in", values]
						}
					},
					callback: function(r) {
						data = r.results;
					}
				});
				return data;
			}
		},
		{
			fieldname: "group_by_1",
			label: __("Group By Level 1"),
			fieldtype: "Select",
			options: ["Ungrouped", "Group by Customer", "Group by Customer Group", "Group by Transaction",
				"Group by Item", "Group by Item Group", "Group by Brand", "Group by Territory", "Group by Sales Person"],
			default: "Ungrouped"
		},
		{
			fieldname: "group_by_2",
			label: __("Group By Level 2"),
			fieldtype: "Select",
			options: ["Ungrouped", "Group by Customer", "Group by Customer Group", "Group by Transaction",
				"Group by Item", "Group by Item Group", "Group by Brand", "Group by Territory", "Group by Sales Person"],
			default: "Group by Customer"
		},
		{
			fieldname: "group_by_3",
			label: __("Group By Level 3"),
			fieldtype: "Select",
			options: ["Ungrouped", "Group by Customer", "Group by Customer Group", "Group by Transaction",
				"Group by Item", "Group by Item Group", "Group by Brand", "Group by Territory", "Group by Sales Person"],
			default: "Group by Transaction"
		},
		{
			fieldname: "group_same_items",
			label: __("Group Same Items"),
			fieldtype: "Check",
			default: 1
		},
		{
			fieldname: "show_basic_values",
			label: __("Show Basic Values"),
			fieldtype: "Check"
		},
		{
			fieldname: "show_tax_exclusive_values",
			label: __("Show Tax Exclusive Values"),
			fieldtype: "Check"
		},
		{
			fieldname: "show_discount_values",
			label: __("Show Discount Values"),
			fieldtype: "Check"
		},
		{
			fieldname: "include_taxes",
			label: __("Show Detailed Taxes"),
			fieldtype: "Check"
		},
	],
	"initial_depth": 1
}


