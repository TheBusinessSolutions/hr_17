# Copyright 2019 Kitti Upariphutthiphong <kittiu@ecosoft.co.th>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import Command, _, api, fields, models
from odoo.exceptions import ValidationError


class HrExpense(models.Model):
    _inherit = "hr.expense"

    advance = fields.Boolean(string="Employee Advance", default=False)
    clearing_product_id = fields.Many2one(
        comodel_name="product.product",
        string="Clearing Product",
        tracking=True,
        domain="[('can_be_expensed', '=', True),"
        "'|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        ondelete="restrict",
        help="Optional: On the clear advance, the clearing "
        "product will create default product line.",
    )
    av_line_id = fields.Many2one(
        comodel_name="hr.expense",
        string="Ref: Advance",
        ondelete="set null",
        help="Expense created from this advance expense line",
    )

    def _get_product_advance(self):
        return self.env.ref("hr_expense_advance_clearing.product_emp_advance", False)

    def _normalize_clearing_line_vals(self, vals):
        if not vals.get("av_line_id"):
            return vals
        vals = dict(vals)
        av_line = self.env["hr.expense"].browse(vals["av_line_id"])
        clearing_product = (
            self.env["product.product"].browse(vals["product_id"])
            if vals.get("product_id")
            else av_line.clearing_product_id or av_line.product_id
        )
        if clearing_product and not vals.get("product_id"):
            vals["product_id"] = clearing_product.id
        if clearing_product and not vals.get("name"):
            vals["name"] = clearing_product.display_name
        if not vals.get("product_uom_id"):
            vals["product_uom_id"] = (
                clearing_product.uom_id.id or av_line.product_uom_id.id
            )
        if not vals.get("quantity"):
            vals["quantity"] = 1.0
        return vals

    def _recompute_clearing_amounts(self):
        clearing_lines = self.filtered("av_line_id")
        if not clearing_lines:
            return
        if hasattr(clearing_lines, "_compute_account_id"):
            clearing_lines._compute_account_id()
        if hasattr(clearing_lines, "_compute_amount"):
            clearing_lines._compute_amount()

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._normalize_clearing_line_vals(vals) for vals in vals_list]
        expenses = super().create(vals_list)
        expenses._recompute_clearing_amounts()
        return expenses

    def write(self, vals):
        vals = self._normalize_clearing_line_vals(vals)
        res = super().write(vals)
        self._recompute_clearing_amounts()
        return res

    @api.constrains("advance")
    def _check_advance(self):
        for expense in self.filtered("advance"):
            emp_advance = expense._get_product_advance()
            if not emp_advance.property_account_expense_id:
                raise ValidationError(
                    _("Employee advance product has no payable account")
                )
            if expense.product_id != emp_advance:
                raise ValidationError(
                    _("Employee advance, selected product is not valid")
                )
            if expense.account_id != emp_advance.property_account_expense_id:
                raise ValidationError(
                    _("Employee advance, account must be the same payable account")
                )
            if expense.tax_ids:
                raise ValidationError(_("Employee advance, all taxes must be removed"))
            if expense.payment_mode != "own_account":
                raise ValidationError(_("Employee advance, paid by must be employee"))
        return True

    @api.onchange("advance")
    def onchange_advance(self):
        self.tax_ids = False
        if self.advance:
            self.product_id = self._get_product_advance()

    def _get_move_line_src(self, move_line_name, partner_id):
        self.ensure_one()
        price_unit = self.price_unit or self.total_amount
        quantity = self.quantity if self.price_unit else 1
        taxes = self.tax_ids.with_context(round=True).compute_all(
            price_unit, self.currency_id, quantity, self.product_id
        )
        amount_currency = self.total_amount_currency - self.tax_amount_currency
        balance = self.total_amount - self.tax_amount
        ml_src_dict = {
            "name": move_line_name,
            "quantity": quantity,
            "debit": balance if balance > 0 else 0,
            "credit": -balance if balance < 0 else 0,
            "amount_currency": amount_currency,
            "account_id": self.account_id.id,
            "product_id": self.product_id.id,
            "product_uom_id": self.product_uom_id.id,
            "analytic_distribution": self.analytic_distribution,
            "expense_id": self.id,
            "partner_id": partner_id,
            "tax_ids": [Command.set(self.tax_ids.ids)],
            "tax_tag_ids": [Command.set(taxes["base_tags"])],
            "currency_id": self.currency_id.id,
        }
        return ml_src_dict

    def _get_move_line_dst(
        self,
        move_line_name,
        partner_id,
        total_amount,
        total_amount_currency,
        account_advance,
    ):
        account_date = (
            self.date
            or self.sheet_id.accounting_date
            or fields.Date.context_today(self)
        )
        ml_dst_dict = {
            "name": move_line_name,
            "debit": total_amount > 0 and total_amount,
            "credit": total_amount < 0 and -total_amount,
            "account_id": account_advance.id,
            "date_maturity": account_date,
            "amount_currency": total_amount_currency,
            "currency_id": self.currency_id.id,
            "expense_id": self.id,
            "partner_id": partner_id,
        }
        return ml_dst_dict
