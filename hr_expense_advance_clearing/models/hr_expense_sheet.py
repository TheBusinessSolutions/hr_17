# Copyright 2019 Kitti Upariphutthiphong <kittiu@ecosoft.co.th>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import Command, _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_compare, float_is_zero
from odoo.tools.safe_eval import safe_eval


class HrExpenseSheet(models.Model):
    _inherit = "hr.expense.sheet"

    # Payment journal (bank, cash, etc.)
    employee_journal_id = fields.Many2one(
        "account.journal",
        string="Payment Journal",
        default=lambda self: self._default_journal_id(),
        check_company=True,
        domain="[('company_id', '=', company_id)]",
    )

    advance = fields.Boolean(string="Employee Advance")
    advance_sheet_id = fields.Many2one(
        "hr.expense.sheet",
        string="Clear Advance",
        domain="[('advance','=',True),('employee_id','=',employee_id),('clearing_residual','>',0.0)]",
        help="Show remaining advance of this employee",
    )
    clearing_sheet_ids = fields.One2many(
        "hr.expense.sheet",
        "advance_sheet_id",
        string="Clearing Sheet",
        readonly=True,
    )
    clearing_count = fields.Integer(compute="_compute_clearing_count")
    clearing_residual = fields.Monetary(
        compute="_compute_clearing_residual",
        string="Amount to clear",
        store=True,
    )
    advance_sheet_residual = fields.Monetary(
        related="advance_sheet_id.clearing_residual",
        string="Advance Remaining",
        store=True,
    )
    amount_payable = fields.Monetary(
        compute="_compute_amount_payable",
        string="Payable Amount",
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

    @api.depends("account_move_ids.line_ids.amount_residual")
    def _compute_clearing_residual(self):
        for sheet in self:
            emp_advance = sheet._get_product_advance()
            residual = 0.0
            if emp_advance:
                for line in sheet.sudo().account_move_ids.line_ids:
                    if line.account_id == emp_advance.property_account_expense_id:
                        residual += line.amount_residual
            sheet.clearing_residual = residual

    def _compute_amount_payable(self):
        for sheet in self:
            rec_lines = sheet.account_move_ids.line_ids.filtered(
                lambda l: l.credit and l.account_id.reconcile and not l.reconciled
            )
            sheet.amount_payable = -sum(rec_lines.mapped("amount_residual"))

    def _compute_clearing_count(self):
        for sheet in self:
            sheet.clearing_count = len(sheet.clearing_sheet_ids)

    def _get_product_advance(self):
        return self.env.ref("hr_expense_advance_clearing.product_emp_advance", False)

    def _get_move_line_vals(self, payment_source_journal=None):
        """Prepare journal entry lines for clearing advance expense"""
        self.ensure_one()
        lines = []
        emp_advance = self._get_product_advance()
        advance_account = emp_advance.property_account_expense_id

        for expense in self.expense_line_ids:
            name = f"{expense.employee_id.name}: {expense.name[:64]}"
            total = expense.total_amount

            # Debit expense account
            lines.append({
                "name": name,
                "debit": total,
                "credit": 0.0,
                "account_id": expense.account_id.id,
                "partner_id": False,
                "currency_id": expense.currency_id.id,
                "amount_currency": total,
            })

            # Credit employee advance account
            lines.append({
                "name": name,
                "debit": 0.0,
                "credit": total,
                "account_id": advance_account.id,
                "partner_id": False,
                "currency_id": expense.currency_id.id,
                "amount_currency": -total,
            })

        # If payment is made directly (cash/bank), create entry for payment source
        if payment_source_journal:
            journal_account = payment_source_journal.default_account_id
            if journal_account:
                total_pay = sum(self.expense_line_ids.mapped("total_amount"))
                lines.append({
                    "name": "Payment",
                    "debit": 0.0,
                    "credit": total_pay,
                    "account_id": journal_account.id,
                    "partner_id": False,
                    "currency_id": self.currency_id.id,
                    "amount_currency": -total_pay,
                })
                lines.append({
                    "name": "Payment",
                    "debit": total_pay,
                    "credit": 0.0,
                    "account_id": advance_account.id,
                    "partner_id": False,
                    "currency_id": self.currency_id.id,
                    "amount_currency": total_pay,
                })

        return lines

    def _prepare_bills_vals(self):
        """Force journal entry for advance clearing"""
        self.ensure_one()
        vals = super()._prepare_bills_vals()
        if self.advance_sheet_id and self.payment_mode == "own_account":
            if self.advance_sheet_residual <= 0.0:
                raise ValidationError(
                    _("Advance %s has no residual amount to clear") % self.name
                )
            vals["move_type"] = "entry"
            # Use employee journal for payment source
            lines = self._get_move_line_vals(payment_source_journal=self.employee_journal_id)
            vals["line_ids"] = [Command.create(x) for x in lines]

            # Remove vendor info
            for k in ("partner_id", "commercial_partner_id", "invoice_date", "invoice_date_due"):
                vals.pop(k, None)
        return vals

    @api.onchange("advance_sheet_id")
    def _onchange_advance_sheet_id(self):
        self.expense_line_ids -= self.expense_line_ids.filtered("av_line_id")
        self.advance_sheet_id.expense_line_ids.sudo().read()
        lines = self.advance_sheet_id.expense_line_ids.filtered("clearing_product_id")
        for line in lines:
            self.expense_line_ids += self.env["hr.expense"].new({
                "advance": False,
                "name": line.clearing_product_id.display_name,
                "product_id": line.clearing_product_id.id,
                "clearing_product_id": False,
                "date": fields.Date.context_today(self),
                "account_id": False,
                "state": "draft",
                "product_uom_id": False,
                "av_line_id": line.id,
            })

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
        """Register payment and reconcile with employee advance"""
        action = super().action_register_payment()
        if self.env.context.get("hr_return_advance"):
            action["context"].update(
                {"clearing_sheet_ids": self.clearing_sheet_ids.ids}
            )
        return action