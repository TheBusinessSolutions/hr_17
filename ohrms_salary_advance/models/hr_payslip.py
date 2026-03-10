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
from odoo import models, _
from odoo.exceptions import UserError


class HrPayslip(models.Model):
    """Extends hr.payslip with Milestone 3 (GL-driven) behaviours.

    get_inputs():
        Reads the live GL balance on the SAR salary rule debit account for
        the employee (DR - CR on posted move lines filtered by partner =
        employee.work_contact_id). This is the real outstanding balance
        the employee owes at the moment the payslip is computed — it
        automatically reflects every advance ever granted and every
        partial or full payment made, whether through a prior payslip
        or a direct cash payment reconciled outside the payslip.

        The accountant sees this figure in the Other Inputs / SAR line
        and can manually reduce it if they want to make a partial
        deduction this month. Whatever they enter is what gets deducted.

    action_payslip_done():
        After super() posts the payslip journal entry, we stamp
        work_contact_id as partner_id on all payslip move lines that
        have no partner yet — consistent with how advance entries are
        posted in Milestone 2.
    """
    _inherit = 'hr.payslip'

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    def _get_sar_rule(self, employee):
        """Return the SAR salary rule for the employee's contract structure,
        or an empty recordset if not found."""
        contract = employee.contract_id
        if not contract or not contract.struct_id:
            return self.env['hr.salary.rule']
        return contract.struct_id.rule_ids.filtered(
            lambda r: r.code == 'SAR'
        )[:1]

    def _get_advance_gl_balance(self, employee):
        """Return the live GL balance (DR - CR) on the SAR advance account
        for the given employee.

        Query:
            account  = SAR salary rule debit account (account_debit)
            partner  = employee.work_contact_id
            state    = posted

        This is the single authoritative figure for how much the employee
        owes at any point in time. It is not derived from advance records —
        it comes directly from the accounting ledger.

        Returns 0.0 if the SAR rule, its account, or the employee partner
        cannot be resolved.
        """
        sar_rule = self._get_sar_rule(employee)
        if not sar_rule or not sar_rule.account_debit_id:
            return 0.0

        partner = employee.work_contact_id
        if not partner:
            return 0.0

        domain = [
            ('account_id', '=', sar_rule.account_debit_id.id),
            ('partner_id', '=', partner.id),
            ('parent_state', '=', 'posted'),
            ('company_id', '=', employee.company_id.id),
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
    # get_inputs — inject live GL balance into SAR line
    # -------------------------------------------------------------------------
    def get_inputs(self, contract_ids, date_from, date_to):
        """Override to populate the SAR input line with the live GL balance
        on the employee's advance account.

        This replaces the original approach of summing advance records filtered
        by month. The GL balance is always accurate because:

          • Every approved advance adds a DR to the advance account.
          • Every payslip deduction adds a CR via the SAR salary rule.
          • Every direct cash payment by the employee adds a CR when
            reconciled against the advance account.

        The accountant sees the correct total in Other Inputs automatically.
        They can reduce it for a partial deduction — whatever they leave in
        the SAR line is what gets deducted from the payslip this month.
        The remaining balance will appear again on the next payslip.
        """
        res = super().get_inputs(contract_ids, date_from, date_to)

        # Resolve employee.
        employee = (
            self.env['hr.contract'].browse(contract_ids[0].id).employee_id
            if contract_ids
            else self.employee_id
        )
        if not employee:
            return res

        gl_balance = self._get_advance_gl_balance(employee)
        if not gl_balance or gl_balance <= 0:
            return res

        # Inject into the SAR line that super() already added at amount 0.
        for result in res:
            if result.get('code') == 'SAR':
                result['amount'] = gl_balance
                break

        return res

    # -------------------------------------------------------------------------
    # action_payslip_done — stamp partner only on the SAR move line
    # -------------------------------------------------------------------------
    def action_payslip_done(self):
        """Override to stamp work_contact_id as partner_id exclusively on
        the SAR salary rule move line in the payslip journal entry.

        Why only SAR:
            All other salary rule lines (Basic, Deductions, NET, etc.) post
            to general expense/liability/bank accounts that belong to the
            company — not to any specific employee. Stamping the employee
            partner on those lines would pollute the partner ledger and
            reports with payroll entries that don't belong there.

            The SAR line posts to the Employee Advance account (1111111)
            which IS per-employee by design — the partner is required there
            so the GL balance query in get_inputs() and the employee ledger
            in Milestone 4 can filter correctly by employee.

        How we identify the SAR line:
            We match by account_id = SAR rule's account_debit_id.
            This is the same account used in _get_advance_gl_balance(),
            so it is guaranteed to be consistent.
        """
        result = super().action_payslip_done()

        for payslip in self:
            partner = payslip.employee_id.work_contact_id
            if not payslip.move_id or not partner:
                continue

            sar_rule = self._get_sar_rule(payslip.employee_id)
            if not sar_rule or not sar_rule.account_debit_id:
                continue

            # Find the SAR line — the line posted on the advance account.
            # We match by account_id only, not by debit/credit direction,
            # because the community module may post it on either side
            # depending on the sign of the SAR amount.
            sar_move_lines = payslip.move_id.line_ids.filtered(
                lambda l: l.account_id == sar_rule.account_debit_id
            )
            if not sar_move_lines:
                continue

            # Reset to draft, stamp partner on SAR lines only, re-post.
            payslip.move_id.button_draft()
            sar_move_lines.write({'partner_id': partner.id})
            payslip.move_id.action_post()

        return result