# Copyright 2019 Kitti Upariphutthiphong <kittiu@ecosoft.co.th>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import Command, _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_compare, float_is_zero
from odoo.tools.safe_eval import safe_eval


class HrExpenseSheet(models.Model):
    _inherit = "hr.expense.sheet"

    # Allow any journal (remove purchase restriction)
    employee_journal_id = fields.Many2one(
        "account.journal",
        string="Journal",
        default=lambda self: self._default_journal_id(),
        check_company=True,
        domain="[('company_id', '=', company_id)]",
    )

    # ---------------------------------------------------------
    # FORCE JOURNAL ENTRY INSTEAD OF VENDOR BILL
    # ---------------------------------------------------------
    def _prepare_bills_vals(self):
        """Create Journal Entry instead of Vendor Bill"""
        self.ensure_one()

        res = super()._prepare_bills_vals()

        # Force accounting entry
        res["move_type"] = "entry"

        # Remove vendor related fields
        res.pop("partner_id", None)
        res.pop("commercial_partner_id", None)
        res.pop("invoice_date", None)
        res.pop("invoice_date_due", None)

        # Advance clearing logic
        if self.advance_sheet_id and self.payment_mode == "own_account":
            if self.advance_sheet_residual <= 0.0:
                raise ValidationError(
                    _("Advance: %s has no amount to clear") % (self.name)
                )

            move_line_vals = self._get_move_line_vals()
            res["line_ids"] = [Command.create(x) for x in move_line_vals]

        return res

    advance = fields.Boolean(
        string="Employee Advance",
    )

    advance_sheet_id = fields.Many2one(
        comodel_name="hr.expense.sheet",
        string="Clear Advance",
        domain="[('advance', '=', True), ('employee_id', '=', employee_id), ('clearing_residual', '>', 0.0)]",
        help="Show remaining advance of this employee",
    )

    clearing_sheet_ids = fields.One2many(
        comodel_name="hr.expense.sheet",
        inverse_name="advance_sheet_id",
        string="Clearing Sheet",
        readonly=True,
        help="Show reference clearing on advance",
    )

    clearing_count = fields.Integer(
        compute="_compute_clearing_count",
    )

    clearing_residual = fields.Monetary(
        string="Amount to clear",
        compute="_compute_clearing_residual",
        store=True,
        help="Amount to clear of this expense sheet in company currency",
    )

    advance_sheet_residual = fields.Monetary(
        string="Advance Remaining",
        related="advance_sheet_id.clearing_residual",
        store=True,
        help="Remaining amount to clear the selected advance sheet",
    )

    amount_payable = fields.Monetary(
        string="Payable Amount",
        compute="_compute_amount_payable",
        help="Final register payment amount even after advance clearing",
    )

    @api.constrains("advance_sheet_id", "expense_line_ids")
    def _check_advance_expense(self):
        advance_lines = self.expense_line_ids.filtered("advance")

        if self.advance_sheet_id and advance_lines:
            raise ValidationError(
                _("Advance clearing must not contain any advance expense line")
            )

        if advance_lines and len(advance_lines) != len(self.expense_line_ids):
            raise ValidationError(_("Advance must contain only advance expense line"))

    @api.depends("account_move_ids.payment_state", "account_move_ids.amount_residual")
    def _compute_from_account_move_ids(self):
        res = super()._compute_from_account_move_ids()

        for sheet in self:
            if (
                sheet.advance_sheet_id
                and sheet.account_move_ids.state == "posted"
                and not sheet.amount_residual
            ):
                sheet.payment_state = "paid"

        return res

    def _get_product_advance(self):
        return self.env.ref(
            "hr_expense_advance_clearing.product_emp_advance", False
        )

    @api.depends("account_move_ids.line_ids.amount_residual")
    def _compute_clearing_residual(self):
        for sheet in self:
            emp_advance = sheet._get_product_advance()
            residual_company = 0.0

            if emp_advance:
                for line in sheet.sudo().account_move_ids.line_ids:
                    if line.account_id == emp_advance.property_account_expense_id:
                        residual_company += line.amount_residual

            sheet.clearing_residual = residual_company

    def _compute_amount_payable(self):
        for sheet in self:
            rec_lines = sheet.account_move_ids.line_ids.filtered(
                lambda x: x.credit and x.account_id.reconcile and not x.reconciled
            )

            sheet.amount_payable = -sum(rec_lines.mapped("amount_residual"))

    def _compute_clearing_count(self):
        for sheet in self:
            sheet.clearing_count = len(sheet.clearing_sheet_ids)

    def action_sheet_move_create(self):
        res = super().action_sheet_move_create()

        for sheet in self:
            if not sheet.advance_sheet_id:
                continue

            amount_residual_bf_reconcile = sheet.advance_sheet_residual

            advance_residual = float_compare(
                amount_residual_bf_reconcile,
                sheet.total_amount,
                precision_rounding=sheet.currency_id.rounding,
            )

            move_lines = (
                sheet.account_move_ids.line_ids
                | sheet.advance_sheet_id.account_move_ids.line_ids
            )

            emp_advance = sheet._get_product_advance()
            account_id = emp_advance.property_account_expense_id.id

            adv_move_lines = (
                self.env["account.move.line"]
                .sudo()
                .search(
                    [
                        ("id", "in", move_lines.ids),
                        ("account_id", "=", account_id),
                        ("reconciled", "=", False),
                    ]
                )
            )

            adv_move_lines.reconcile()

            if advance_residual != -1:
                sheet.write({"state": "done"})
            else:
                sheet.write(
                    {
                        "state": "post",
                        "payment_state": "not_paid",
                        "amount_residual": sheet.total_amount
                        - amount_residual_bf_reconcile,
                    }
                )

        return res

    def open_clear_advance(self):
        self.ensure_one()

        action = self.env.ref(
            "hr_expense_advance_clearing.action_hr_expense_sheet_advance_clearing"
        )

        vals = action.sudo().read()[0]

        context1 = vals.get("context", {})

        if context1:
            context1 = safe_eval(context1)

        context1["default_advance_sheet_id"] = self.id
        context1["default_employee_id"] = self.employee_id.id

        vals["context"] = context1

        return vals

    def action_open_clearings(self):
        self.ensure_one()

        return {
            "name": _("Clearing Sheets"),
            "type": "ir.actions.act_window",
            "res_model": "hr.expense.sheet",
            "view_mode": "tree,form",
            "domain": [("id", "in", self.clearing_sheet_ids.ids)],
        }

    def action_register_payment(self):
        action = super().action_register_payment()

        if self.env.context.get("hr_return_advance"):
            action["context"].update(
                {"clearing_sheet_ids": self.clearing_sheet_ids.ids}
            )

        return action