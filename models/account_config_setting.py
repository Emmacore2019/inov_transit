# -*- coding: utf-8 -*-

from odoo import models, fields, api
from datetime import datetime, timedelta, date
from ast import literal_eval


class PartnerTransit(models.Model):
    _inherit = 'res.partner'

    regime_type = fields.Selection(
        [('simple', 'Simple'), ('placam', 'PLACAM'), ('sorepco', 'SOREPCO'), ('cimaf', 'CIMAF')],
        string="Type de Facture", default='simple'
    )
    percent = fields.Float("Taux", default=0.2)
    caution = fields.Float("Caution", default=2500)
    rc = fields.Char("RCCM")
    nui = fields.Char("NUI")
    folder_ids = fields.One2many('folder.transit', 'customer_id', string="Les Dossiers")
    folder_count = fields.Integer(
        string='Nombre de Dossier',
        compute='_get_count_folder',
        store=True
    )
    shipping_count = fields.Integer(
        string='Nombre de Shipping',
        compute='_get_count_folder',
        store=True
    )
    acconnage_count = fields.Integer(
        string='Nombre de Acconage',
        compute='_get_count_folder',
        store=True
    )

    @api.depends('folder_ids')
    def _get_count_folder(self):
        for record in self:
            foldercount = len(record.folder_ids.filtered(lambda r: r.stages == 'transit'))
            acconagecount = len(record.folder_ids.filtered(lambda r: r.stages == 'accone'))
            shipcount = len(record.folder_ids.filtered(lambda r: r.stages == 'ship'))
            record.update({
                'folder_count': foldercount,
                'shipping_count': shipcount,
                'acconnage_count': acconagecount,
            })

    @api.onchange('regime_type')
    def _onchange_regime(self):
        if self.regime_type == "simple":
            self.percent = 0.2
            self.caution = 2500
        elif self.regime_type == "placam":  # Ajoutez des valeurs spécifiques si nécessaire
            self.percent = 0.32  # Ajustez selon votre logique
            self.caution = 5000
        elif self.regime_type == "sorepco":  # Ajoutez des valeurs spécifiques si nécessaire
            self.percent = 0.25  # Ajustez selon votre logique
            self.caution = 3000
        elif self.regime_type == "cimaf":  # Ajoutez des valeurs spécifiques si nécessaire
            self.percent = 0.15  # Ajustez selon votre logique
            self.caution = 2000

    def open_partner_history_transit(self):
        '''
        Cette fonction retourne une action qui affiche les dossiers de transit pour le partenaire donné.
        '''
        action = self.env.ref('inov_transit.action_transit_folder').read()[0]
        action['domain'] = literal_eval(action['domain'])
        action['domain'].append(('customer_id', 'child_of', self.id))
        return action

    def open_partner_history_accone(self):
        '''
        Cette fonction retourne une action qui affiche les dossiers d'acconage pour le partenaire donné.
        '''
        action = self.env.ref('inov_transit.action_acconage_folder').read()[0]
        action['domain'] = literal_eval(action['domain'])
        action['domain'].append(('customer_id', 'child_of', self.id))
        return action

class Prestations(models.Model):
    _name = 'prestation.transit'

    name = fields.Char("Description")
    product_id = fields.Many2one(
        'product.product',
        string='Service',
        required=True,
        domain="[('sale_ok', '=', True),('type','=','service')]")
    type_service = fields.Selection([('fixed', 'Fixe'), ('changed', 'Variable')], string="Type de service",
                                    default='fixed')
    caution = fields.Float("Caution", required=True, default=0.0)
    taux = fields.Float("Taux", required=True, default=0.0)


class InvoiceProductTransit(models.Model):
    _name = 'invoice.transit'
    _rec_name = 'vendor_id'
    @api.model
    def _default_currency(self):
        return self.env.user.company_id.currency_id

    vendor_id = fields.Many2one(
        'res.partner',
        string='Fournisseur',
        required=True
    )

    folder_id = fields.Many2one(
        'folder.transit',
        string='Dossier',
    )
    total_fac_fob = fields.Monetary("Total FOB", currency_field='foreign_currency_id', store=True)
    total_fac_fret = fields.Monetary("Total FRET", currency_field='foreign_currency_id', store=True)
    currency_id = fields.Many2one('res.currency', string='Currency',
                                  required=True, readonly=True,
                                  default=_default_currency, tracking=True)
    foreign_currency_id = fields.Many2one('res.currency', string='Foreign Currency',
                                          required=True,
                                          tracking=True)
    currency_rate = fields.Float('Taux', digits=(12, 4), store=True)
    weighty_all = fields.Float('Poids Total de la cargaison', required=True)
    chiffr_xaf_take = fields.Monetary("Valeur Imposable", currency_field='currency_id', store=True,
                                      compute='compute_total_contain_product')
    uom_total_id = fields.Many2one(
        'uom.uom',
        string='Unite de Mesure',
    )
    package = fields.Many2one(
        'package.transit',
        string='Conteneur'
    )
    num_package = fields.Char("Numero du Conteneur")
    assurance = fields.Monetary("ASSURANCE", currency_field='currency_id', compute='compute_assurance_total',
                                store=True)

    line_ids = fields.One2many('product.transit', 'invoice_id', string='Produits Transportes')

    @api.onchange('foreign_currency_id', 'currency_id')
    def _onchange_foreign_currency(self):
        if self.foreign_currency_id:
            company = self.env.user.company_id
            self.currency_rate = self.foreign_currency_id._get_conversion_rate(self.foreign_currency_id,
                                                                               self.currency_id, company,
                                                                               fields.Date.today())
        # for line in self:
        #     if line.foreign_currency_id:
        #         line.currency_rate = line.currency_id.rate/line.foreign_currency_id.rate

    @api.onchange('folder_id')
    def _onchange_customer_id(self):
        if self.folder_id:
            self.vendor_id = self.folder_id.vendor_id


    @api.depends('vendor_id', 'total_fac_fob', 'total_fac_fret', 'foreign_currency_id')
    def compute_assurance_total(self):
        company = self.env.user.company_id
        if self.vendor_id.regime_type == 'simple':
            val_fob = self.foreign_currency_id._convert(self.total_fac_fob * self.vendor_id.percent / 100,
                                                        self.currency_id, company, fields.Date.today())
            total_fob = (val_fob + self.vendor_id.caution) * 19.25 / 100
            self.assurance = val_fob + total_fob + self.vendor_id.caution + 600
        else:
            val_fob = self.foreign_currency_id._convert(
                (self.total_fac_fob + self.total_fac_fret) * self.vendor_id.percent / 100, self.currency_id, company,
                fields.Date.today())
            total_fob = (val_fob + self.vendor_id.caution) * 19.25 / 100
            self.assurance = val_fob + total_fob + self.vendor_id.caution + 600


    @api.depends('line_ids', 'foreign_currency_id')
    def compute_total_contain_product(self):
        company = self.env.user.company_id
        self.chiffr_xaf_take = self.foreign_currency_id._convert(
            round(sum(line.chiffr_xaf_take if line.chiffr_xaf_take else line.chiffr_xaf for line in self.line_ids)),
            self.currency_id, company, fields.Date.today())

    @api.model
    def create(self, values):
        result = super(InvoiceProductTransit, self).create(values)
        return result


class OrderTransit(models.Model):
    _name = 'product.transit'

    @api.depends('invoice_id')
    def _compute_foreign_currency(self):
        return self.invoice_id.foreign_currency_id

    ref = fields.Char(string="N° FACTURE")
    product_id = fields.Many2one(
        'product.product',
        string='Nature produit',
        required=True,
        domain="[('sale_ok', '=', True)]")

    invoice_id = fields.Many2one("invoice.transit", string="Facture")
    position_tarif = fields.Char("Position tarifaire")
    description = fields.Text("Commentaire")
    origin = fields.Char("Origin")
    regime = fields.Char(string="Regime")
    weight_brut_qty = fields.Float("Poids Brut")
    weight_net_qty = fields.Float("Poids Net")
    fac_fob = fields.Monetary("FOB", currency_field='foreign_currency_id', store=True)
    fac_fret = fields.Monetary("FRET", currency_field='foreign_currency_id', store=True)
    fac_cfr = fields.Monetary("CFR", currency_field='foreign_currency_id', store=True, compute='compute_cfr_converter')
    # fac_cfr_xaf= fields.Monetary("CFR", currency_field='foreign_currency_id',store=True,compute='compute_cfr_converter')
    assurance = fields.Monetary("ASSURANCE", currency_field='foreign_currency_id', compute='compute_assurance',
                                store=True)
    chiffr_xaf = fields.Monetary("CAF", currency_field='foreign_currency_id', store=True, readonly=True,
                                 compute='compute_caf')
    chiffr_xaf_take = fields.Monetary("CAF A PRENDRE", currency_field='foreign_currency_id', store=True)
    uom_brut_id = fields.Many2one(
        'uom.uom',
        string='Unit Brute',
    )
    uom_net_id = fields.Many2one(
        'uom.uom',
        string='Unit Net',
    )
    nbre_package = fields.Integer("Nombre de Colis")
    company_currency_id = fields.Many2one('res.currency', related='company_id.currency_id', string="Company Currency",
                                          readonly=True)

    foreign_currency_id = fields.Many2one('res.currency', string='Foreign Currency',
                                          required=True, compute='compute_assurance',
                                          tracking=True)
    company_id = fields.Many2one('res.company', string='Company', change_default=True,
                                 required=True, readonly=True, default=lambda self: self.env.user.company_id)
    file_upload = fields.Binary('Fichier Telecharge')
    file_invoice = fields.Char('Invioice Name')


    @api.depends('invoice_id', 'weight_brut_qty')
    def compute_assurance(self):
        for record in self:
            if record.invoice_id:
                record.foreign_currency_id = record.invoice_id.foreign_currency_id
                currency = self.env.user.company_id.currency_id
                assurance_foreign = currency._convert(record.invoice_id.assurance, record.foreign_currency_id,
                                                      record.company_id, fields.Date.today())
                record.assurance = round((assurance_foreign * record.weight_brut_qty) / record.invoice_id.weighty_all,
                                         2)


    @api.depends('fac_fob', 'fac_fret', 'foreign_currency_id')
    def compute_cfr_converter(self):
        for record in self:
            if record.foreign_currency_id:
                record.fac_cfr = record.fac_fob + record.fac_fret
                # self.fac_cfr_xaf=self.currency_rate * self.fac_cfr


    @api.depends('fac_cfr', 'assurance')
    def compute_caf(self):
        for record in self:
            record.chiffr_xaf = record.fac_cfr + record.assurance




