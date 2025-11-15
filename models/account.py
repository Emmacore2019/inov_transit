from odoo import models, fields, api, _
from . import conversion
from odoo.exceptions import UserError
from odoo.tools import email_re, email_split, email_escape_char, float_is_zero, float_compare, \
    pycompat, date_utils

MAP_INVOICE_TYPE_PARTNER_TYPE = {
    'out_invoice': 'customer',
    'out_refund': 'customer',
    'in_invoice': 'supplier',
    'in_refund': 'supplier',
}


class TransitAccountInvoice(models.Model):
    _inherit = 'account.move'


    @api.depends('amount_total')
    def get_amount_letter(self):
        currency = "FCFA"
        amount = conversion.trad(self.amount_total, currency)
        return amount


    @api.depends('invoice_line_ids')
    def get_invoice_line_changed(self):
        prestation = self.env['prestation.transit']
        invoices_change = []
        invoices = []
        for line in self.invoice_line_ids:
            if prestation.search([('product_id', '=', line.product_id.id)]):
                invoices_change.append(line)
            else:
                invoices.append(line)
        return invoices_change, invoices

    @api.depends('service_ids.amount_debour', 'invoice_line_ids.price_total')
    def _compute_amount_transit(self):
        prestation = self.env['prestation.transit']
        for transit in self:
            amount_debour = amount_folder = amount_variable = 0.0
            for line in transit.service_ids:
                amount_debour += line.amount_debour
            for folder in transit.invoice_line_ids:
                if prestation.search([('product_id', '=', folder.product_id.id)]):
                    amount_variable += folder.price_total
                elif prestation.search([('product_id', '=', folder.product_id.id), ('type_service', '=', 'fixed')]):
                    amount_variable += folder.price_total
                else:
                    amount_folder += folder.price_total
            transit.update({
                'amount_transit_debours': transit.currency_id.round(amount_debour),
                'amount_transit_expense': transit.currency_id.round(amount_folder),
                'amount_transit_expense_variable': transit.currency_id.round(amount_variable),
            })

    sent = fields.Boolean(string='Sent', default=False) #je viens d'ajouter cette ligne pour installer le champ sent dans le model account.move

    transit_id = fields.Many2one('folder.transit', string='Dossier')
    date_op = fields.Date(string='Date Operation')
    number_d = fields.Char(string='Numero')
    service_ids = fields.Many2many('debour.transit')
    folder_type = fields.Selection([('transit', 'Dedouanement'), ('accone', 'Acconage'), ('ship', 'Shipping')],
                                   string="Processus")
    amount_transit_debours = fields.Monetary("Debours", compute='_compute_amount_transit', currency_field='currency_id',
                                             store=True, tracking=True)
    amount_transit_expense = fields.Monetary("Autres Charges", compute='_compute_amount_transit',
                                             currency_field='currency_id', store=True, tracking=True)
    amount_transit_expense_variable = fields.Monetary("Charges Variables", compute='_compute_amount_transit',
                                                      currency_field='currency_id', store=True,
                                                      tracking=True)


    def invoice_print(self):
        """ Print the invoice and mark it as sent, so that we can see more
            easily the next step of the workflow
        """
        self.filtered(lambda inv: not inv.sent).write({'sent': True})
        if self.user_has_groups('account.group_account_invoice') and self.partner_id.regime_type == 'simple':
            return self.env.ref('inov_transit.account_invoice_folder_simple_id').report_action(self)
        if self.user_has_groups('account.group_account_invoice') and self.partner_id.regime_type == 'sorepco':
            return self.env.ref('inov_transit.account_invoices_folder_transit_sorepco').report_action(self)
        if self.user_has_groups('account.group_account_invoice') and self.partner_id.regime_type == 'placam':
            return self.env.ref('inov_transit.account_invoices_folder_transit_placam').report_action(self)
        if self.user_has_groups('account.group_account_invoice') and self.partner_id.regime_type == 'cimaf':
            return self.env.ref('inov_transit.account_invoices_folder_transit_cimaf').report_action(self)
        return self.env.ref('account.account_invoices').report_action(self)


    # @api.depends('invoice_line_ids.price_subtotal', 'tax_ids.amount', 'tax_ids.amount_rounding',
    #              'currency_id', 'company_id', 'date_invoice', 'type', 'service_ids.amount_debour')
    # def _compute_amount(self):
    #     round_curr = self.currency_id.round
    #     self.amount_untaxed = sum(line.price_subtotal for line in self.invoice_line_ids)
    #     self.amount_tax = sum(round_curr(line.amount_total) for line in self.tax_ids)
    #     self.amount_total = self.amount_transit_expense_variable + self.amount_transit_expense + self.amount_transit_debours
    #     amount_total_company_signed = self.amount_total
    #     amount_untaxed_signed = self.amount_untaxed
    #     if self.currency_id and self.company_id and self.currency_id != self.company_id.currency_id:
    #         currency_id = self.currency_id
    #         amount_total_company_signed = currency_id._convert(self.amount_total, self.company_id.currency_id,
    #                                                            self.company_id,
    #                                                            self.date_invoice or fields.Date.today())
    #         amount_untaxed_signed = currency_id._convert(self.amount_untaxed, self.company_id.currency_id,
    #                                                      self.company_id, self.date_invoice or fields.Date.today())
    #     sign = self.type in ['in_refund', 'out_refund'] and -1 or 1
    #     self.amount_total_company_signed = amount_total_company_signed * sign
    #     self.amount_total_signed = self.amount_total * sign
    #     self.amount_untaxed_signed = amount_untaxed_signed * sign

    def compute_variable_expense(self):
        self.ensure_one()
        transitLine = self.env['account.move.line']

        for folder in self:
            if folder.service_ids:
                commission = self.env.ref('inov_transit.view_prestation_transit_fixed')
                if commission.product_id.id:
                    account_id = self.fiscal_position_id.map_account(
                        commission.product_id.property_account_income_id or commission.product_id.categ_id.property_account_income_categ_id).id
                commission_dict = {
                    'invoice_id': folder.id,
                    'folder_id': folder.transit_id.id,
                    'name': commission.product_id.name,
                    'account_id': account_id,
                    'price_unit': commission.product_id.lst_price,
                    'product_id': commission.product_id.id,
                    'quantity': sum(f.amount_debour for f in folder.service_ids),
                    'caution': commission.caution,
                    'invoice_line_tax_ids': [(6, 0, commission.product_id.taxes_id.ids)],
                }
                transitLine.create(commission_dict)
            if folder.transit_id.amount_purchased:
                honoraire = self.env.ref('inov_transit.view_prestation_transit_changed')
                if honoraire.product_id.id:
                    account_id = self.fiscal_position_id.map_account(
                        honoraire.product_id.property_account_income_id or honoraire.product_id.categ_id.property_account_income_categ_id).id
                honoraire_dict = {
                    'invoice_id': folder.id,
                    'folder_id': folder.transit_id.id,
                    'price_unit': honoraire.product_id.lst_price,
                    'account_id': account_id,
                    'name': honoraire.product_id.name,
                    'product_id': honoraire.product_id.id,
                    'quantity': folder.transit_id.amount_purchased,
                    'caution': honoraire.caution,
                    'invoice_line_tax_ids': [(6, 0, honoraire.product_id.taxes_id.ids)],
                }
                transitLine.create(honoraire_dict)
            folder.compute_taxes()

    def get_taxes_values(self):
        tax_grouped = {}
        round_curr = self.currency_id.round
        Prestation_obj = self.env['prestation.transit']

        for line in self.invoice_line_ids:
            product_fixed = Prestation_obj.search(
                [('product_id', '=', line.product_id.id), ('type_service', '=', 'fixed')])
            product_changed = Prestation_obj.search(
                [('product_id', '=', line.product_id.id), ('type_service', '=', 'changed')])
            if not line.account_id:
                continue
            if product_fixed:
                quantity = (product_fixed.taux / 100.0) * line.quantity + line.caution
            elif product_changed:
                quantity = (product_changed.taux / 100.0) * line.quantity + line.caution
            else:
                quantity = line.quantity

            price_unit = line.price_unit * (1 - (line.discount or 0.0) / 100.0)
            taxes = line.invoice_line_tax_ids.compute_all(price_unit, self.currency_id, quantity, line.product_id,
                                                          self.partner_id)['taxes']
            for tax in taxes:

                val = self._prepare_tax_line_vals(line, tax)
                key = self.env['account.tax'].browse(tax['id']).get_grouping_key(val)

                if key not in tax_grouped:
                    tax_grouped[key] = val
                    tax_grouped[key]['base'] = round_curr(val['base'])
                else:
                    tax_grouped[key]['amount'] += val['amount']
                    tax_grouped[key]['base'] += round_curr(val['base'])
        return tax_grouped

    @api.model
    def debour_line_move_line_get(self):
        res = []
        if self.service_ids:
            product = self.env.ref('inov_transit.product_product_debours')
            account = product.product_tmpl_id._get_product_accounts()
            move_line_dict = {
                'invl_id': self.id,
                'type': 'src',
                'name': product.name,
                'price_unit': product.lst_price,
                'quantity': 1.0,
                'price': self.amount_transit_debours,
                'account_id': account['income'].id,
                'product_id': product.id,
                'uom_id': product.uom_id.id,
                'invoice_id': self.id,
            }
            res.append(move_line_dict)
        return res


    def action_move_create(self):
        """ Creates invoice related analytics and financial move lines """
        account_move = self.env['account.move']

        for inv in self:
            if not inv.journal_id.sequence_id:
                raise UserError(_('Please define sequence on the journal related to this invoice.'))
            if not inv.invoice_line_ids.filtered(lambda line: line.account_id):
                raise UserError(_('Please add at least one invoice line.'))
            if inv.move_id:
                continue

            if not inv.date_invoice:
                inv.write({'date_invoice': fields.Date.context_today(self)})
            if not inv.date_due:
                inv.write({'date_due': inv.date_invoice})
            company_currency = inv.company_id.currency_id

            # create move lines (one per invoice line + eventual taxes and analytic lines)
            iml = inv.invoice_line_move_line_get()
            iml += inv.tax_line_move_line_get()
            iml += inv.debour_line_move_line_get()
            diff_currency = inv.currency_id != company_currency
            # create one move line for the total and possibly adjust the other lines amount
            total, total_currency, iml = inv.compute_invoice_totals(company_currency, iml)

            name = inv.name or ''
            if inv.payment_term_id:
                totlines = \
                inv.payment_term_id.with_context(currency_id=company_currency.id).compute(total, inv.date_invoice)[0]
                res_amount_currency = total_currency
                for i, t in enumerate(totlines):
                    if inv.currency_id != company_currency:
                        amount_currency = company_currency._convert(t[1], inv.currency_id, inv.company_id,
                                                                    inv._get_currency_rate_date() or fields.Date.today())
                    else:
                        amount_currency = False

                    # last line: add the diff
                    res_amount_currency -= amount_currency or 0
                    if i + 1 == len(totlines):
                        amount_currency += res_amount_currency

                    iml.append({
                        'type': 'dest',
                        'name': name,
                        'price': t[1],
                        'account_id': inv.account_id.id,
                        'date_maturity': t[0],
                        'amount_currency': diff_currency and amount_currency,
                        'currency_id': diff_currency and inv.currency_id.id,
                        'invoice_id': inv.id
                    })
            else:
                iml.append({
                    'type': 'dest',
                    'name': name,
                    'price': total,
                    'account_id': inv.account_id.id,
                    'date_maturity': inv.date_due,
                    'amount_currency': diff_currency and total_currency,
                    'currency_id': diff_currency and inv.currency_id.id,
                    'invoice_id': inv.id
                })
            part = self.env['res.partner']._find_accounting_partner(inv.partner_id)
            line = [(0, 0, self.line_get_convert(l, part.id)) for l in iml]
            line = inv.group_lines(iml, line)

            line = inv.finalize_invoice_move_lines(line)

            date = inv.date or inv.date_invoice
            move_vals = {
                'ref': inv.reference,
                'line_ids': line,
                'journal_id': inv.journal_id.id,
                'date': date,
                'narration': inv.comment,
            }
            move = account_move.create(move_vals)
            # Pass invoice in method post: used if you want to get the same
            # account move reference when creating the same invoice after a cancelled one:
            move.post(invoice=inv)
            # make the invoice point to that move
            vals = {
                'move_id': move.id,
                'date': date,
                'move_name': move.name,
            }
            inv.write(vals)
        return True


class TransitExpense(models.Model):
    _inherit = 'account.move.line'

    folder_id = fields.Many2one(
        'folder.transit',
        string='Dossier',
        ondelete='cascade',
        index=True,
        copy=False
    )

    caution = fields.Float("Caution", default=0.0, tracking=True, reaonly=True)

    Tarif = fields.Char(string='Tarif', compute='_get_tarif')

    @api.onchange('product_id')
    def _onchange_price_unit(self):
        Prestation_obj = self.env['prestation.transit']
        product_fixed = Prestation_obj.search([('product_id', '=', self.product_id.id), ('type_service', '=', 'fixed')])
        product_changed = Prestation_obj.search(
            [('product_id', '=', self.product_id.id), ('type_service', '=', 'changed')])
        if product_fixed:
            self.caution = product_fixed.caution
        if product_changed:
            self.caution = product_changed.caution


    @api.depends('product_id')
    def _get_tarif(self):
        Prestation_obj = self.env['prestation.transit']
        product_fixed = Prestation_obj.search([('product_id', '=', self.product_id.id), ('type_service', '=', 'fixed')])
        product_changed = Prestation_obj.search(
            [('product_id', '=', self.product_id.id), ('type_service', '=', 'changed')])
        if product_fixed:
            self.Tarif = str(product_fixed.taux) + "%"
        if product_changed:
            self.Tarif = str(product_changed.caution) + "+" + str(product_changed.taux)


    @api.depends('price_unit', 'discount', 'invoice_line_tax_ids', 'quantity',
                 'product_id', 'invoice_id.partner_id', 'invoice_id.currency_id', 'invoice_id.company_id',
                 'invoice_id.date_invoice', 'invoice_id.date', 'caution')
    def _compute_price(self):
        quantity = 0.0
        currency = self.invoice_id and self.invoice_id.currency_id or None
        Prestation_obj = self.env['prestation.transit']
        product_fixed = Prestation_obj.search([('product_id', '=', self.product_id.id), ('type_service', '=', 'fixed')])
        product_changed = Prestation_obj.search(
            [('product_id', '=', self.product_id.id), ('type_service', '=', 'changed')])
        if product_fixed:
            quantity = (product_fixed.taux / 100.0) * self.quantity + self.caution
        elif product_changed:
            quantity = (product_changed.taux / 100.0) * self.quantity + self.caution

        else:
            quantity = self.quantity

        price = self.price_unit * (1 - (self.discount or 0.0) / 100.0)

        taxes = False
        if self.invoice_line_tax_ids:
            taxes = self.invoice_line_tax_ids.compute_all(price, currency, quantity, product=self.product_id,
                                                          partner=self.invoice_id.partner_id)
        # if product_fixed:
        #     self.price_subtotal = price_subtotal_signed =  ((self.quantity * price) + self.caution) * (1 - (self.discount or 0.0) / 100.0)
        #     taxes=product_fixed.product_id.taxes_id
        #     self.price_total = sum(self.price_subtotal*(1 + (t.amount/100)) for t in taxes) if taxes else self.price_subtotal
        # elif product_changed:
        #     self.price_subtotal = price_subtotal_signed =  (self.quantity * price + self.caution)* (1 - (self.discount or 0.0) / 100.0)
        #     taxes=product_changed.product_id.taxes_id
        #     self.price_total = sum(self.price_subtotal*(1 + (t.amount/100)) for t in taxes) if taxes else self.price_subtotal
        # else:
        self.price_subtotal = price_subtotal_signed = taxes['total_excluded'] if taxes else quantity * price
        self.price_total = taxes['total_included'] if taxes else self.price_subtotal

        if self.invoice_id.currency_id and self.invoice_id.currency_id != self.invoice_id.company_id.currency_id:
            currency = self.invoice_id.currency_id
            date = self.invoice_id._get_currency_rate_date()
            price_subtotal_signed = currency._convert(price_subtotal_signed, self.invoice_id.company_id.currency_id,
                                                      self.company_id or self.env.user.company_id,
                                                      date or fields.Date.today())
        sign = self.invoice_id.type in ['in_refund', 'out_refund'] and -1 or 1
        self.price_subtotal_signed = price_subtotal_signed * sign


class TransitAccountPayment(models.Model):
    _inherit = 'account.payment'

    folder_type = fields.Selection([('transit', 'Dedouanement'), ('accone', 'Acconage'), ('ship', 'Shipping')],
                                   string="Processus")

    @api.model
    def default_get(self, fields):
        res = super(TransitAccountPayment, self).default_get(fields)
        active_ids = self._context.get('active_ids')
        active_model = self._context.get('active_model')

        # Check for selected invoices ids
        if not active_ids or active_model != 'account.move':
            return res

        invoices = self.env['account.move'].browse(active_ids)

        # Check all invoices are open
        if any(invoice.state != 'open' for invoice in invoices):
            raise UserError(_("You can only register payments for open invoices"))
        # Check all invoices have the same currency
        if any(inv.currency_id != invoices[0].currency_id for inv in invoices):
            raise UserError(_("In order to pay multiple invoices at once, they must use the same currency."))
        # Check if, in batch payments, there are not negative invoices and positive invoices
        dtype = invoices[0].type
        for inv in invoices[1:]:
            if inv.type != dtype:
                if ((dtype == 'in_refund' and inv.type == 'in_invoice') or
                        (dtype == 'in_invoice' and inv.type == 'in_refund')):
                    raise UserError(
                        _("You cannot register payments for vendor bills and supplier refunds at the same time."))
                if ((dtype == 'out_refund' and inv.type == 'out_invoice') or
                        (dtype == 'out_invoice' and inv.type == 'out_refund')):
                    raise UserError(
                        _("You cannot register payments for customer invoices and credit notes at the same time."))

        # Look if we are mixin multiple commercial_partner or customer invoices with vendor bills
        multi = any(inv.commercial_partner_id != invoices[0].commercial_partner_id
                    or MAP_INVOICE_TYPE_PARTNER_TYPE[inv.type] != MAP_INVOICE_TYPE_PARTNER_TYPE[invoices[0].type]
                    or inv.account_id != invoices[0].account_id
                    or inv.partner_bank_id != invoices[0].partner_bank_id
                    for inv in invoices)

        currency = invoices[0].currency_id

        total_amount = self._compute_payment_amount(invoices=invoices, currency=currency)

        res.update({
            'amount': abs(total_amount),
            'currency_id': currency.id,
            'payment_type': total_amount > 0 and 'inbound' or 'outbound',
            'partner_id': False if multi else invoices[0].commercial_partner_id.id,
            'partner_type': False if multi else MAP_INVOICE_TYPE_PARTNER_TYPE[invoices[0].type],
            'communication': ' '.join([ref for ref in invoices.mapped('reference') if ref])[:2000],
            'invoice_ids': [(6, 0, invoices.ids)],
            'multi': multi,
            'folder_type': invoices[0].folder_type,
        })
        return res


    def post(self):
        for invoice in self.invoice_ids.filtered(lambda ml: ml.state == 'paid'):
            if invoice.transit_id.number >= 105 and invoice.transit_id.stages == 'transit':
                transit = invoice.transit_id
                search_domain = [('number', '=', 106)]
                nws_stage = transit._stage_find(search_domain)
                values = transit._onchange_stage_id_values(nws_stage.id)
                transit.write({'stage_id': nws_stage.id})
                transit.update(values)
                invoice.update({
                    'folder_type': invoice.transit_id.stages
                })
            elif invoice.transit_id.stages == 'ship':
                number = invoice.transit_id.number
                folder = invoice.transit_id
                search_domain = [('number', '=', number)]
                nws_stage = folder._stage_find(search_domain)
                values = folder._onchange_stage_id_values(nws_stage.id)
                transit.write({'stage_id': nws_stage.id})
                transit.update(values)
                invoice.update({
                    'folder_type': invoice.transit_id.stages
                })
            elif invoice.transit_id.stages == 'accone':
                number = invoice.transit_id.number
                folder = invoice.transit_id
                search_domain = [('number', '=', number)]
                nws_stage = folder._stage_find(search_domain)
                values = folder._onchange_stage_id_values(nws_stage.id)
                transit.write({'stage_id': nws_stage.id})
                transit.update(values)
                invoice.update({
                    'folder_type': invoice.transit_id.stages
                })
            else:
                raise UserError("Le Dossier doit etre a l'etat Valide pour")

        return super(TransitAccountPayment, self).post()