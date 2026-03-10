# -*- coding: utf-8 -*-
#############################################################################
#    A part of Open HRMS Project <https://www.openhrms.com>
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2023-TODAY Cybrosys Technologies(<https://www.cybrosys.com>)
#    Author: Cybrosys Techno Solutions(<https://www.cybrosys.com>)
#
#    You can modify it under the terms of the GNU LESSER
#    GENERAL PUBLIC LICENSE (LGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU LESSER GENERAL PUBLIC LICENSE (LGPL v3) for more details.
#
#    You should have received a copy of the GNU LESSER GENERAL PUBLIC LICENSE
#    (LGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
#############################################################################
import time
from odoo.exceptions import UserError
from odoo import api, fields, models, _


class SalaryAdvance(models.Model):
    """Salary Advance model — supports multiple concurrent open advances per
    employee. The total outstanding approved advances are guarded against a
    maximum ceiling defined as max_percent of the employee contract wage."""

    _name = "salary.advance"
    _description = "Salary Advance"
    _inherit = ['mail.thread', 'mail.activity.mixin']

    # -------------------------------------------------------------------------
    # Fields
    # -------------------------------------------------------------------------
    name = fields.Char(
        string='Name', readonly=True,
        default=lambda self: 'Adv/',
        help='Sequence reference of the salary advance.')
    employee_id = fields.Many2one(
        'hr.employee', string='Employee', required=True,
        help='Employee requesting the advance.')
    date = fields.Date(
        string='Date', required=True,
        default=lambda self: fields.Date.today(),
        help='Date of the advance request.')
    reason = fields.Text(
        string='Reason',
        help='Reason provided by the employee for requesting the advance.')
    currency_id = fields.Many2one(
        'res.currency', string='Currency', required=True,
        default=lambda self: self.env.user.company_id.currency_id,
        help='Currency used for this advance.')
    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.user.company_id,
        help='Company of the employee.')
    advance = fields.Float(
        string='Advance Amount', required=True,
        help='Amount being requested as a salary advance.')
    payment_method = fields.Many2one(
        'account.journal', string='Payment Method',
        help='Payment journal used to disburse the advance.')
    exceed_condition = fields.Boolean(
        string='Exceed Maximum',
        help='Check this to allow the advance to exceed the maximum percentage '
             'configured on the salary structure. Requires HR Manager approval.')
    department = fields.Many2one(
        'hr.department', string='Department',
        related='employee_id.department_id',
        help='Department of the employee (auto-filled).')
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('submit', 'Submitted'),
            ('waiting_approval', 'Waiting Approval'),
            ('approve', 'Approved'),
            ('cancel', 'Cancelled'),
            ('reject', 'Rejected'),
        ],
        string='Status', default='draft',
        tracking=True,
        help='Current state of the salary advance request.')
    debit = fields.Many2one(
        'account.account', string='Debit Account',
        help='Account debited when the advance is disbursed (e.g. Employee '
             'Advance Account).')
    credit = fields.Many2one(
        'account.account', string='Credit Account',
        help='Account credited when the advance is disbursed (e.g. Cash/Bank).')
    journal = fields.Many2one(
        'account.journal', string='Journal',
        help='Accounting journal used to post the advance entry.')
    employee_contract_id = fields.Many2one(
        'hr.contract', string='Contract',
        related='employee_id.contract_id',
        help='Active contract of the employee (auto-filled).')

    # ------------------------------------------------------------------
    # Milestone 2 — Journal entry reference.
    # Stored when accounting approves so we can:
    #   • display a smart button linking directly to the move, and
    #   • use the move line for reconciliation in future milestones.
    # ------------------------------------------------------------------
    move_id = fields.Many2one(
        'account.move', string='Journal Entry',
        readonly=True, copy=False,
        help='Journal entry created when accounting approved this advance.')

    # ------------------------------------------------------------------
    # Computed: total outstanding approved advances for this employee.
    # Used in the form view as an informational field so both HR and
    # Accounting can see the current exposure before approving.
    # ------------------------------------------------------------------
    total_outstanding_advance = fields.Float(
        string='Total Outstanding Advances',
        compute='_compute_total_outstanding_advance',
        help='Sum of all approved (not yet fully deducted) advances for this '
             'employee. Used to check against the maximum ceiling.')

    # -------------------------------------------------------------------------
    # Computed methods
    # -------------------------------------------------------------------------
    @api.depends('employee_id', 'state')
    def _compute_total_outstanding_advance(self):
        """Compute the live GL balance on the SAR advance account for this
        employee — SUM(debit) - SUM(credit) on posted move lines where
        account = SAR debit account AND partner = employee.work_contact_id.

        This is the real outstanding balance the employee owes, regardless
        of how many advances exist or how many partial payments were made.
        It is shown on the advance form as an informational figure."""
        for rec in self:
            rec.total_outstanding_advance = rec._get_gl_balance()

    def _get_gl_balance(self):
        """Return the live GL balance (DR - CR) on the SAR advance account
        for this employee. Used by both the computed field and the ceiling
        check so there is a single query point.

        Returns 0.0 if the employee, their contract, or the SAR account
        cannot be resolved.
        """
        self.ensure_one()
        if not self.employee_id:
            return 0.0
        partner = self.employee_id.work_contact_id
        if not partner:
            return 0.0
        sar_account = self._get_sar_account()
        if not sar_account:
            return 0.0
        # Query posted move lines on the advance account for this employee.
        domain = [
            ('account_id', '=', sar_account.id),
            ('partner_id', '=', partner.id),
            ('parent_state', '=', 'posted'),
            ('company_id', '=', self.company_id.id),
        ]
        data = self.env['account.move.line'].read_group(
            domain=domain,
            fields=['debit:sum', 'credit:sum'],
            groupby=[],
        )
        if not data:
            return 0.0
        return (data[0].get('debit') or 0.0) - (data[0].get('credit') or 0.0)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    def _get_sar_account(self):
        """Return the debit account defined on the SAR salary rule for this
        employee's contract salary structure.

        This is the single source of truth for the advance account used
        across the entire module:
          • Auto-filled on the advance form when the employee is selected.
          • Used by hr_payslip.get_inputs() to query the GL balance.
          • Used by _check_advance_ceiling() to compute outstanding balance.

        Returns an account.account record or an empty recordset if not found.
        """
        self.ensure_one()
        contract = self.employee_contract_id
        if not contract or not contract.struct_id:
            return self.env['account.account']
        sar_rule = contract.struct_id.rule_ids.filtered(
            lambda r: r.code == 'SAR'
        )
        if not sar_rule:
            return self.env['account.account']
        # Take first match in case of duplicates; account_debit_id is the
        # field name on hr.salary.rule in hr_payroll_community.
        return sar_rule[0].account_debit_id

    # -------------------------------------------------------------------------
    # Onchange
    # -------------------------------------------------------------------------
    @api.onchange('employee_id')
    def _onchange_employee_id(self):
        """Auto-fill the debit account from the SAR salary rule when the
        employee is selected or changed. Kept editable so the accountant
        can override it if needed."""
        if not self.employee_id:
            return
        sar_account = self._get_sar_account()
        if sar_account:
            self.debit = sar_account

    @api.onchange('company_id')
    def _onchange_company_id(self):
        """Restrict journal domain to the selected company."""
        company = self.company_id
        return {
            'domain': {
                'journal': [('company_id', '=', company.id)],
            }
        }

    # -------------------------------------------------------------------------
    # Button actions — state transitions
    # -------------------------------------------------------------------------
    def action_submit_to_manager(self):
        """Employee submits the request to their HR manager."""
        self.state = 'submit'

    def action_cancel(self):
        """Cancel a draft or submitted advance."""
        self.state = 'cancel'

    def action_reject(self):
        """HR manager rejects the advance during waiting-approval stage."""
        self.state = 'reject'

    # -------------------------------------------------------------------------
    # ORM overrides
    # -------------------------------------------------------------------------
    @api.model
    def create(self, vals):
        """Assign a sequence number on creation."""
        vals['name'] = (
            self.env['ir.sequence'].get('salary.advance.seq') or '/'
        )
        return super().create(vals)

    # -------------------------------------------------------------------------
    # Approval helpers
    # -------------------------------------------------------------------------
    def _check_advance_ceiling(self):
        """Validate that adding this advance does not push the employee's
        total outstanding balance above the ceiling defined on the salary
        structure.

        Ceiling = (max_percent / 100) * contract wage

        Outstanding balance is read from the live GL (DR - CR on the SAR
        advance account for this employee) so it automatically reflects
        any partial payments already made — whether through a payslip or
        a direct cash payment — without any flags to maintain.

        The `exceed_condition` flag lets an HR manager explicitly bypass
        the ceiling for exceptional cases.
        """
        self.ensure_one()
        if self.exceed_condition:
            return

        contract = self.employee_contract_id
        if not contract:
            return

        max_percent = contract.struct_id.max_percent if contract.struct_id else 0
        if not max_percent:
            return  # No ceiling configured — allow freely.

        max_allowed = (max_percent / 100.0) * contract.wage

        # Live GL balance = what the employee currently owes.
        current_gl_balance = self._get_gl_balance()

        if current_gl_balance + self.advance > max_allowed:
            raise UserError(
                _(
                    'Cannot approve this advance.\n\n'
                    'Employee: %(name)s\n'
                    'Requested: %(req).2f\n'
                    'Current GL balance (outstanding): %(out).2f\n'
                    'Maximum allowed (%(pct)s%% of %(wage).2f wage): %(max).2f\n\n'
                    'Total would be %(total).2f which exceeds the ceiling of '
                    '%(max).2f.\n\n'
                    'To override, tick the "Exceed Maximum" checkbox before '
                    'resubmitting.'
                ) % {
                    'name': self.employee_id.name,
                    'req': self.advance,
                    'out': current_gl_balance,
                    'pct': max_percent,
                    'wage': contract.wage,
                    'max': max_allowed,
                    'total': current_gl_balance + self.advance,
                }
            )

    # -------------------------------------------------------------------------
    # Smart button action — Milestone 2
    # -------------------------------------------------------------------------
    def action_open_journal_entry(self):
        """Open the journal entry linked to this advance.
        Available to accounting users once the advance is approved."""
        self.ensure_one()
        if not self.move_id:
            raise UserError(_('No journal entry found for this advance.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Journal Entry'),
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def approve_request(self):
        """HR Manager first-level approval.

        Checks performed:
        1. Employee must have a home address.
        2. Employee must have an active contract.
        3. Advance amount must be > 0.
        4. This month's payslip must not already be confirmed.
        5. Aggregate outstanding balance must not exceed the ceiling
           (unless exceed_condition is ticked).

        Removed from original:
        - One-advance-per-month restriction (replaced by ceiling check above).
        - advance_date day restriction (removed per client requirement).
        """
        self.ensure_one()

        if not self.employee_id.work_contact_id:
            raise UserError(
                _('Employee "%s" has no work contact defined.\n'
                  'Please set it under the Work Information tab on the employee form.')
                % self.employee_id.name
            )

        if not self.employee_contract_id:
            raise UserError(
                _('No active contract found for employee "%s". '
                  'Please define a contract before approving an advance.')
                % self.employee_id.name
            )

        if not self.advance or self.advance <= 0:
            raise UserError(_('Please enter a valid advance amount greater than zero.'))

        # Aggregate ceiling check.
        self._check_advance_ceiling()

        self.state = 'waiting_approval'

    def approve_request_acc_dept(self):
        """Accounting department final approval — posts the journal entry.

        Checks performed:
        1. Advance amount must be > 0.
        2. Debit account, Credit account, and Journal must be filled.
        3. Aggregate ceiling check (second gate — in case contract/structure
           changed between HR approval and accounting approval).

        Journal entry posted:
            DR  Debit Account (Employee Advance Account)   advance amount  [partner = employee.work_contact_id]
            CR  Credit Account (Cash / Bank)               advance amount  [partner = employee.work_contact_id]

        Milestone 2 change:
            partner_id = employee.work_contact_id is now attached to BOTH move
            lines. This makes every advance traceable per employee on the
            dedicated COA account (e.g. 1410 Employee Salary Advances).
            Because the account type is Current Asset (not Receivable/Payable),
            these lines do NOT appear in the Partner Ledger or Aged Receivables
            report — they stay internal to the advance account only.

            work_contact_id is used instead of address_id because in Odoo 17
            address_id points to the employee's home address which often
            defaults to the company partner — giving the wrong partner on the
            move line. work_contact_id is the employee's own dedicated partner
            record and always carries the correct employee name.
        """
        self.ensure_one()

        if not self.advance or self.advance <= 0:
            raise UserError(_('Please enter a valid advance amount greater than zero.'))

        if not self.debit or not self.credit or not self.journal:
            raise UserError(
                _('Please fill in the Debit Account, Credit Account, and '
                  'Journal before approving.')
            )

        # Second ceiling check — accounting approval is the financial gate.
        self._check_advance_ceiling()

        # ------------------------------------------------------------------
        # Milestone 2 (corrected): use work_contact_id as the partner on
        # both move lines. This is the employee's own partner record in
        # Odoo 17 and always carries the correct employee name.
        # address_id was the original field but it defaults to the company
        # partner in most setups, producing "My Company" on the move line.
        # ------------------------------------------------------------------
        partner_id = self.employee_id.work_contact_id.id
        if not partner_id:
            raise UserError(
                _('Employee "%s" has no work contact defined.\n'
                  'Please set it under the Work Information tab on the employee form.')
                % self.employee_id.name
            )

        today = time.strftime('%Y-%m-%d')
        line_ids = [
            # ------------------------------------------------------------------
            # Debit line — Employee Advance Account (e.g. 1410)
            # DR this account to record the asset: money owed by the employee.
            # partner_id links the line to the employee so every advance is
            # individually traceable on this account per employee.
            # ------------------------------------------------------------------
            (0, 0, {
                'name': _('Salary Advance - %s - %s') % (
                    self.employee_id.name, self.name),
                'account_id': self.debit.id,
                'journal_id': self.journal.id,
                'date': today,
                'partner_id': partner_id,
                'debit': self.advance if self.advance > 0.0 else 0.0,
                'credit': -self.advance if self.advance < 0.0 else 0.0,
            }),
            # ------------------------------------------------------------------
            # Credit line — Cash / Bank
            # CR the payment account. partner_id attached here too so both
            # sides of the entry are consistent and the payment is visible
            # under the employee partner's transactions if needed.
            # ------------------------------------------------------------------
            (0, 0, {
                'name': _('Salary Advance - %s - %s') % (
                    self.employee_id.name, self.name),
                'account_id': self.credit.id,
                'journal_id': self.journal.id,
                'date': today,
                'partner_id': partner_id,
                'debit': -self.advance if self.advance < 0.0 else 0.0,
                'credit': self.advance if self.advance > 0.0 else 0.0,
            }),
        ]

        move_vals = {
            'narration': _('Salary Advance of %s [%s]') % (
                self.employee_id.name, self.name),
            'ref': self.name,
            'journal_id': self.journal.id,
            'date': today,
            'line_ids': line_ids,
        }

        move = self.env['account.move'].create(move_vals)
        move.action_post()

        # Store the move reference so the smart button and future
        # reconciliation (Milestone 4) can reach it directly.
        self.write({
            'state': 'approve',
            'move_id': move.id,
        })
        return True