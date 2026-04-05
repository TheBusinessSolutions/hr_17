# Copyright 2019 Kitti Upariphutthiphong <kittiu@ecosoft.co.th>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import Command, _, api, fields, models
from odoo.exceptions import ValidationError, UserError
from odoo.tools import float_compare, float_is_zero
from odoo.tools.safe_eval import safe_eval


class HrExpenseSheet(models.Model):
    _inherit = "hr.expense.sheet"
    
    # Remove domain restriction and allow custom journal
    employee_journal_id = fields.Many2one(
        "account.journal",
        string="Journal",
        default=lambda self: self._default_journal_id(),
        check_company=True,
        domain="[('company_id', '=', company_id)]",
    )

    advance = fields.Boolean(
        string="Employee Advance",
    )
    advance_sheet_id = fields.Many2one(
        comodel_name="hr.expense.sheet",
        string="Clear Advance",
        domain="[('advance', '=', True), ('employee_id', '=', employee_id),"
        " ('clearing_residual', '>', 0.0)]",
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
    
    # NEW FIELDS FOR NEW WORKFLOW
    advance_payment_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string="Payment Source (Bank/Cash)",
        domain="[('company_id', '=', company_id), ('type', 'in', ['bank', 'cash'])]",
        help="Select the bank or cash journal for the advance payment",
        copy=False,
    )
    advance_confirmed = fields.Boolean(
        string="Advance Confirmed",
        default=False,
        copy=False,
        help="Indicates if advance has been confirmed by finance",
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
        """After clear advance.
        if amount residual is zero, payment state will change to 'paid'
        """
        res = super()._compute_from_account_move_ids()
        for sheet in self:
            if sheet.advance and not sheet.advance_sheet_id and sheet.account_move_ids.filtered(
                lambda move: move.state == "posted"
            ):
                # Advance sheets are already paid to the employee when their
                # payment entry is posted. The residual then represents the
                # amount left to clear, not an unpaid balance.
                sheet.payment_state = "paid"
                continue
            if (
                sheet.advance_sheet_id
                and sheet.account_move_ids.state == "posted"
                and not sheet.amount_residual
            ):
                sheet.payment_state = "paid"
        return res

    def _get_product_advance(self):
        return self.env.ref("hr_expense_advance_clearing.product_emp_advance", False)

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

    # NEW METHOD: Confirm advance (replaces Post Journal Entries for advances)
    def action_confirm_advance(self):
        """Confirm the advance - no journal entry created yet"""
        self.ensure_one()
        if not self.advance:
            raise UserError(_("This action is only for employee advances"))
        if self.state != 'approve':
            raise UserError(_("Only approved advances can be confirmed"))
        
        # Mark as confirmed
        self.write({'advance_confirmed': True})
        return True

    # NEW METHOD: Pay advance - creates journal entry directly
    def action_pay_advance(self):
        """Create payment and journal entry for advance"""
        self.ensure_one()
        
        if not self.advance:
            raise UserError(_("This action is only for employee advances"))
        
        if not self.advance_confirmed:
            raise UserError(_("Please confirm the advance first"))
        
        if not self.advance_payment_journal_id:
            raise UserError(_("Please select a payment source (Bank/Cash)"))
        
        # Create the journal entry directly
        move = self._create_advance_payment_entry()
        
        # Post the move
        move.action_post()
        
        # Update state
        self.write({'state': 'done', 'payment_state': 'paid'})
        
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payment Entry'),
            'res_model': 'account.move',
            'res_id': move.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    def _create_advance_payment_entry(self):
        """Create journal entry for advance payment
        Debit: Employee Advance Account (from expense line)
        Credit: Bank/Cash Account (from payment journal)
        """
        self.ensure_one()
        
        emp_advance = self._get_product_advance()
        if not emp_advance or not emp_advance.property_account_expense_id:
            raise UserError(_("Employee Advance product has no expense account configured"))
        
        advance_account = emp_advance.property_account_expense_id
        payment_account = self.advance_payment_journal_id.default_account_id
        
        if not payment_account:
            raise UserError(_("Payment journal %s has no default account") % self.advance_payment_journal_id.name)
        
        # Prepare move lines
        partner_id = self.employee_id.sudo().work_contact_id.id
        move_lines = []
        
        # Debit: Employee Advance
        move_lines.append(Command.create({
            'name': _('Employee Advance: %s') % self.employee_id.name,
            'account_id': advance_account.id,
            'partner_id': partner_id,
            'debit': self.total_amount,
            'credit': 0.0,
            'currency_id': self.currency_id.id,
        }))
        
        # Credit: Bank/Cash
        move_lines.append(Command.create({
            'name': _('Advance Payment: %s') % self.name,
            'account_id': payment_account.id,
            'partner_id': partner_id,
            'debit': 0.0,
            'credit': self.total_amount,
            'currency_id': self.currency_id.id,
        }))
        
        # Create the move
        move_vals = {
            'name': '/',
            'journal_id': self.advance_payment_journal_id.id,
            'date': fields.Date.context_today(self),
            'ref': self.name,
            'move_type': 'entry',
            'expense_sheet_id': self.id,
            'line_ids': move_lines,
        }
        
        move = self.env['account.move'].create(move_vals)
        return move

    def action_sheet_move_create(self):
        """Override to prevent creating moves for unconfirmed advances"""
        # For advances, skip the normal flow if using new workflow
        if self.advance and not self.advance_sheet_id:
            # This is an advance payment - use new workflow
            if not self.advance_confirmed:
                raise UserError(_(
                    "For advances, please use 'Confirm Advance' button first, "
                    "then select payment source and use 'Pay Advance' button"
                ))
            # If confirmed but not paid yet, show error
            if not self.account_move_ids:
                raise UserError(_(
                    "Please use 'Pay Advance' button to create the payment entry"
                ))
        
        # For clearings and regular expenses, continue with normal flow
        res = super().action_sheet_move_create()
        
        # Handle clearing reconciliation
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
            # Update state on clearing advance when advance residual > total amount
            if advance_residual != -1:
                sheet.write(
                    {
                        "state": "done",
                    }
                )
            # Update amount residual and state when advance residual < total amount
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

    def _get_move_line_vals(self):
        self.ensure_one()
        move_line_vals = []
        advance_to_clear = self.advance_sheet_residual
        emp_advance = self._get_product_advance()
        account_advance = emp_advance.property_account_expense_id
        for expense in self.expense_line_ids:
            move_line_name = (
                expense.employee_id.name + ": " + expense.name.split("\n")[0][:64]
            )
            total_amount = 0.0
            total_amount_currency = 0.0
            partner_id = expense.employee_id.sudo().work_contact_id.id
            # source move line
            move_line_src = expense._get_move_line_src(move_line_name, partner_id)
            move_line_values = [move_line_src]
            total_amount -= expense.total_amount
            total_amount_currency -= expense.total_amount_currency

            # destination move line
            move_line_dst = expense._get_move_line_dst(
                move_line_name,
                partner_id,
                total_amount,
                total_amount_currency,
                account_advance,
            )
            # Check clearing > advance, it will split line
            credit = move_line_dst["credit"]
            # cr payable -> cr advance
            remain_payable = 0.0
            payable_move_line = []
            rounding = expense.currency_id.rounding
            if (
                float_compare(
                    credit,
                    advance_to_clear,
                    precision_rounding=rounding,
                )
                == 1
            ):
                remain_payable = credit - advance_to_clear
                move_line_dst["credit"] = advance_to_clear
                move_line_dst["amount_currency"] = -advance_to_clear
                advance_to_clear = 0.0
                # extra payable line
                payable_move_line = move_line_dst.copy()
                payable_move_line["credit"] = remain_payable
                payable_move_line["amount_currency"] = -remain_payable
                payable_move_line[
                    "account_id"
                ] = expense.sheet_id._get_expense_account_destination()
            else:
                advance_to_clear -= credit
            # Add destination first (if credit is not zero)
            if not float_is_zero(move_line_dst["credit"], precision_rounding=rounding):
                move_line_values.append(move_line_dst)
            if payable_move_line:
                move_line_values.append(payable_move_line)
            move_line_vals.extend(move_line_values)
        return move_line_vals

    def _get_clearing_journal(self):
        """Get the appropriate journal for advance clearing entries.
        
        Priority order:
        1. Miscellaneous/General journal (type 'general')
        2. First available journal
        """
        self.ensure_one()
        
        # Find a general/miscellaneous journal
        general_journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        
        if general_journal:
            return general_journal
        
        # Fallback to any available journal
        return self.env['account.journal'].search([
            ('company_id', '=', self.company_id.id),
        ], limit=1)

    def _prepare_bills_vals(self):
        """create journal entry instead of bills when clearing document"""
        self.ensure_one()
        res = super()._prepare_bills_vals()
        
        # For advance clearing (has advance_sheet_id)
        if self.advance_sheet_id and self.payment_mode == "own_account":
            if (
                self.advance_sheet_residual <= 0.0
            ):  # Advance Sheets with no residual left
                raise ValidationError(
                    _("Advance: %s has no amount to clear") % (self.name)
                )
            # Set as journal entry and use appropriate journal
            res["move_type"] = "entry"
            clearing_journal = self._get_clearing_journal()
            res["journal_id"] = clearing_journal.id
            move_line_vals = self._get_move_line_vals()
            res["line_ids"] = [Command.create(x) for x in move_line_vals]
        
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

    def get_domain_advance_sheet_expense_line(self):
        return self.advance_sheet_id.expense_line_ids.filtered("clearing_product_id")

    def create_clearing_expense_line(self, line):
        clear_advance = self._prepare_clear_advance(line)
        clearing_line = self.env["hr.expense"].new(clear_advance)
        return clearing_line

    @api.onchange("advance_sheet_id")
    def _onchange_advance_sheet_id(self):
        self.expense_line_ids -= self.expense_line_ids.filtered("av_line_id")
        self.advance_sheet_id.expense_line_ids.sudo().read()  # prefetch
        lines = self.get_domain_advance_sheet_expense_line()
        for line in lines:
            self.expense_line_ids += self.create_clearing_expense_line(line)

    def _prepare_clear_advance(self, line):
        # Prepare the clearing expense
        clear_line_dict = {
            "advance": False,
            "name": line.clearing_product_id.display_name,
            "product_id": line.clearing_product_id.id,
            "clearing_product_id": False,
            "date": fields.Date.context_today(self),
            "account_id": False,
            "state": "draft",
            "product_uom_id": False,
            "av_line_id": line.id,
        }
        clear_line = self.env["hr.expense"].new(clear_line_dict)
        clear_line._compute_account_id()  # Set some vals
        # Prepare the original advance line
        adv_dict = line._convert_to_write(line._cache)
        # remove no update columns
        _fields = line._fields
        del_cols = [k for k in _fields.keys() if _fields[k].type == "one2many"]
        del_cols += list(self.env["mail.thread"]._fields.keys())
        del_cols += list(self.env["mail.activity.mixin"]._fields.keys())
        del_cols += list(clear_line_dict.keys())
        del_cols += [
            "price_unit",
            "quantity",
            "total_amount",
            "total_amount_company",
            "total_amount_currency",
            "tax_amount",
            "tax_amount_company",
            "tax_amount_currency",
        ]
        del_cols = list(set(del_cols))
        adv_dict = {k: v for k, v in adv_dict.items() if k not in del_cols}
        # Assign the known value from original advance line
        clear_line.update(adv_dict)
        # Keep editable pricing inputs on the generated clearing line so
        # unit price can be saved and the amount fields can recompute.
        clear_line.price_unit = line.price_unit
        clear_line.quantity = line.quantity or 1.0
        if hasattr(clear_line, "_compute_amount"):
            clear_line._compute_amount()
        clearing_dict = clear_line._convert_to_write(clear_line._cache)
        # Convert list of int to [(6, 0, list)]
        clearing_dict = {
            k: isinstance(v, list)
            and all(isinstance(x, int) for x in v)
            and [(6, 0, v)]
            or v
            for k, v in clearing_dict.items()
        }
        return clearing_dict

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
                {
                    "clearing_sheet_ids": self.clearing_sheet_ids.ids,
                }
            )
        return action
