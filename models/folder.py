from odoo import models, fields, api, SUPERUSER_ID, _
from odoo.exceptions import UserError,ValidationError
from odoo.tools import float_is_zero, float_compare, DEFAULT_SERVER_DATETIME_FORMAT

from datetime import datetime, timedelta, date
import logging

_logger = logging.getLogger(__name__)

AVAILABLE_PRIORITIES = [
    ('0', 'Low'),
    ('1', 'Medium'),
    ('2', 'High'),
    ('3', 'Very High'),
]


class TransitFolder(models.Model):
    _name = "folder.transit"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'analytic.mixin']
    _description = "Dossier de Transit"
    _order = "date_open desc"


    @api.depends('task_checklist', 'stages')
    def _get_checklist_progress(self):
        """:return the value for the check list progress"""
        for rec in self:
            # Calculer le nombre total de tâches pour cette étape
            total_activities = rec.env['mail.activity.type'].search_count([('stages', '=', rec.stages)])
            total_len = total_activities or rec.len_task or 1  # Éviter la division par zéro
            
            # Compter les tâches accomplies
            check_list_len = len(rec.task_checklist)
            
            # Calculer le pourcentage
            rec.checklist_progress = (check_list_len * 100.0) / total_len

    @api.onchange('stages')
    def _onchange_stages_update_len_task(self):
        """Met à jour len_task quand l'étape change"""
        if self.stages:
            total_activities = self.env['mail.activity.type'].search_count([('stages', '=', self.stages)])
            self.len_task = total_activities

    @api.model
    def _default_currency(self):
        return self.env.user.company_id.currency_id

    @api.model
    def _default_number(self):
        stage_id = self._get_default_stage_id()
        values = self._onchange_stage_id_values(stage_id.id)
        if 'number' in values.keys():
            return values['number']
        return 0

    def _get_default_stage_id(self):
        """ Gives default stage_id """
        stage = self.env.context.get('default_stages')
        return self._stage_find(domain=[('number', '=', 10), ('stages', '=', stage)])[0]

    @api.depends('debour_ids')
    def _compute_service_count(self):
        for record in self:
            record.service_count = len(record.debour_ids) if record.debour_ids else 0

    @api.depends('order_ids')
    def get_total_order_ids_amount(self):
        self.amount_purchased = sum(a.chiffr_xaf_take for a in self.order_ids)

    @api.depends('date_arrival')
    def compute_alerte_date(self):
        for record in self:
            old_alerte = record.alerte
            if not record.date_arrival:
                record.alerte = 'open'
            elif record.date_arrival:
                date_today = datetime.today()
                eta_date = datetime.strptime(record.date_arrival.strftime("%Y-%m-%d"), "%Y-%m-%d")
                next_date = date_today + timedelta(days=3)

                if eta_date < date_today:
                    record.alerte = 'overdue'
                elif eta_date <= next_date:
                    record.alerte = 'danger'
                else:
                    record.alerte = 'open'
            else:
                record.alerte = 'open'
            
            # Notification automatique si changement d'état
            if old_alerte != record.alerte and record.id:
                record._notify_alerte_change(old_alerte, record.alerte)

    def compute_deadline_date(self, date, duree):
        if not date:
            date = self.create_date
        date_today = datetime.strptime(date.strftime("%Y-%m-%d"), "%Y-%m-%d")
        next_date = date_today + timedelta(days=duree)
        return next_date.strftime("%Y-%m-%d")

    def _notify_alerte_change(self, old_alerte, new_alerte):
        """Notifie les changements d'état d'alerte"""
        if not self.id or old_alerte == new_alerte:
            return
            
        # Messages selon le changement d'état
        messages = {
            ('open', 'danger'): "⚠️ Attention : Le dossier {name} entre en phase d'alerte (ETA dans 3 jours ou moins)",
            ('open', 'overdue'): "🚨 Urgent : Le dossier {name} est maintenant en retard (ETA dépassé)",
            ('danger', 'overdue'): "🚨 Critique : Le dossier {name} est maintenant en retard (ETA dépassé)",
            ('danger', 'open'): "✅ Le dossier {name} n'est plus en alerte",
            ('overdue', 'open'): "✅ Le dossier {name} n'est plus en retard",
            ('overdue', 'danger'): "⚠️ Le dossier {name} n'est plus en retard mais reste en alerte"
        }
        
        message_key = (old_alerte, new_alerte)
        if message_key in messages:
            message = messages[message_key].format(name=self.name)
            
            # Notification interne
            self.message_post(
                body=message,
                message_type='notification',
                subtype_xmlid='mail.mt_note'
            )
            
            # Programmer une activité si passage en alerte ou retard
            if new_alerte in ['danger', 'overdue'] and old_alerte == 'open':
                self._schedule_alerte_activity(new_alerte)

    def _schedule_alerte_activity(self, alerte_type):
        """Programme une activité selon le type d'alerte"""
        if alerte_type == 'overdue':
            activity_type = self.env.ref('inov_transit.mail_activity_alerte_overdue', False)
            summary = f"🚨 URGENT - Dossier {self.name} en retard"
            note = f"Le dossier {self.name} est en retard. L'ETA était le {self.date_arrival}. Action immédiate requise."
        elif alerte_type == 'danger':
            activity_type = self.env.ref('inov_transit.mail_activity_alerte_danger', False)
            summary = f"⚠️ ATTENTION - Dossier {self.name} en alerte"
            note = f"Le dossier {self.name} arrive bientôt à échéance. ETA: {self.date_arrival}. Préparation requise."
        else:
            return
            
        if activity_type:
            self.activity_schedule(
                act_type_xmlid=activity_type.xml_id,
                summary=summary,
                note=note,
                user_id=self.user_id.id or self.env.user.id,
                date_deadline=fields.Date.today()
            )

    @api.model
    def action_update_all_alertes(self):
        """Action serveur pour mettre à jour toutes les alertes et envoyer un rapport par email"""
        folders = self.search([('date_arrival', '!=', False)])
        updated_count = 0
        
        # Dictionnaires pour collecter les dossiers par état
        overdue_folders = []
        danger_folders = []
        new_overdue = []
        new_danger = []
        
        for folder in folders:
            old_alerte = folder.alerte
            folder.compute_alerte_date()
            
            # Collecter les dossiers par état actuel
            if folder.alerte == 'overdue':
                overdue_folders.append(folder)
                if old_alerte != 'overdue':
                    new_overdue.append(folder)
            elif folder.alerte == 'danger':
                danger_folders.append(folder)
                if old_alerte != 'danger':
                    new_danger.append(folder)
            
            if old_alerte != folder.alerte:
                updated_count += 1
        
        # Envoyer le rapport par email si nécessaire
        if overdue_folders or danger_folders:
            self._send_alerte_report(overdue_folders, danger_folders, new_overdue, new_danger)
                
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Mise à jour des alertes',
                'message': f'{updated_count} dossier(s) mis à jour. Rapport envoyé par email.',
                'type': 'success',
            }
        }

    def _send_alerte_report(self, overdue_folders, danger_folders, new_overdue, new_danger):
        """Envoie un rapport par email avec les dossiers en alerte"""
        # Configuration des destinataires
        all_users = set()
        for folder in overdue_folders + danger_folders:
            if folder.user_id and folder.user_id.email:
                all_users.add(folder.user_id)
        
        # Ajouter les managers de transit
        transit_managers = self.env.ref('inov_transit.group_transit_manager', False)
        if transit_managers:
            for user in transit_managers.users:
                if user.email:
                    all_users.add(user)
        
        if not all_users:
            return
        
        # Génération du rapport HTML
        today = fields.Date.today()
        
        html_content = f"""
        <div style="font-family: Arial, sans-serif; max-width: 1000px; margin: 0 auto;">
            <div style="background-color: #f8f9fa; padding: 20px; border-radius: 8px; margin-bottom: 20px;">
                <h2 style="color: #dc3545; margin-top: 0;">
                    🚨 Rapport d'alertes ETA - {today.strftime('%d/%m/%Y')}
                </h2>
                <p style="font-size: 16px; color: #6c757d;">
                    Mise à jour automatique des états d'avancement des dossiers
                </p>
            </div>
            
            <!-- Résumé statistique -->
            <div style="background-color: #e9ecef; padding: 15px; border-radius: 5px; margin-bottom: 20px;">
                <h3 style="margin-top: 0;">📊 Résumé</h3>
                <div style="display: flex; justify-content: space-around; text-align: center;">
                    <div>
                        <div style="font-size: 24px; font-weight: bold; color: #dc3545;">{len(overdue_folders)}</div>
                        <div>En retard</div>
                    </div>
                    <div>
                        <div style="font-size: 24px; font-weight: bold; color: #ffc107;">{len(danger_folders)}</div>
                        <div>En alerte</div>
                    </div>
                </div>
            </div>
        """
        
        # Section dossiers en retard
        if overdue_folders:
            html_content += f"""
            <div style="background-color: #f8d7da; border: 1px solid #f5c6cb; padding: 15px; border-radius: 5px; margin-bottom: 20px;">
                <h3 style="color: #721c24; margin-top: 0;">
                    🚨 Dossiers en retard ({len(overdue_folders)})
                </h3>
                <p style="color: #721c24; margin-bottom: 15px;">
                    Les dossiers suivants ont dépassé leur ETA et nécessitent une action immédiate :
                </p>
                <table style="width: 100%; border-collapse: collapse; background-color: white;">
                    <thead style="background-color: #dc3545; color: white;">
                        <tr>
                            <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">N° Dossier</th>
                            <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">Client</th>
                            <th style="padding: 12px; text-align: center; border: 1px solid #dee2e6;">ETA</th>
                            <th style="padding: 12px; text-align: center; border: 1px solid #dee2e6;">Retard (jours)</th>
                            <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">Responsable</th>
                            <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">N° B/L</th>
                        </tr>
                    </thead>
                    <tbody>
            """
            
            for folder in overdue_folders:
                retard_jours = (today - folder.date_arrival).days if folder.date_arrival else 0
                html_content += f"""
                        <tr style="border-bottom: 1px solid #dee2e6;">
                            <td style="padding: 10px; border: 1px solid #dee2e6;"><strong>{folder.name}</strong></td>
                            <td style="padding: 10px; border: 1px solid #dee2e6;">{folder.customer_id.name or 'N/A'}</td>
                            <td style="padding: 10px; text-align: center; border: 1px solid #dee2e6;">{folder.date_arrival.strftime('%d/%m/%Y') if folder.date_arrival else 'N/A'}</td>
                            <td style="padding: 10px; text-align: center; color: #dc3545; font-weight: bold; border: 1px solid #dee2e6;">{retard_jours}</td>
                            <td style="padding: 10px; border: 1px solid #dee2e6;">{folder.user_id.name or 'Non assigné'}</td>
                            <td style="padding: 10px; border: 1px solid #dee2e6;">{folder.num_brd or 'N/A'}</td>
                        </tr>
                """
            
            html_content += """
                    </tbody>
                </table>
            </div>
            """
        
        # Section dossiers en alerte
        if danger_folders:
            html_content += f"""
            <div style="background-color: #fff3cd; border: 1px solid #ffeaa7; padding: 15px; border-radius: 5px; margin-bottom: 20px;">
                <h3 style="color: #856404; margin-top: 0;">
                    ⚠️ Dossiers en alerte ({len(danger_folders)})
                </h3>
                <p style="color: #856404; margin-bottom: 15px;">
                    Les dossiers suivants arrivent à échéance dans les 3 jours :
                </p>
                <table style="width: 100%; border-collapse: collapse; background-color: white;">
                    <thead style="background-color: #ffc107; color: #212529;">
                        <tr>
                            <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">N° Dossier</th>
                            <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">Client</th>
                            <th style="padding: 12px; text-align: center; border: 1px solid #dee2e6;">ETA</th>
                            <th style="padding: 12px; text-align: center; border: 1px solid #dee2e6;">Jours restants</th>
                            <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">Responsable</th>
                            <th style="padding: 12px; text-align: left; border: 1px solid #dee2e6;">N° B/L</th>
                        </tr>
                    </thead>
                    <tbody>
            """
            
            for folder in danger_folders:
                jours_restants = (folder.date_arrival - today).days if folder.date_arrival else 0
                html_content += f"""
                        <tr style="border-bottom: 1px solid #dee2e6;">
                            <td style="padding: 10px; border: 1px solid #dee2e6;"><strong>{folder.name}</strong></td>
                            <td style="padding: 10px; border: 1px solid #dee2e6;">{folder.customer_id.name or 'N/A'}</td>
                            <td style="padding: 10px; text-align: center; border: 1px solid #dee2e6;">{folder.date_arrival.strftime('%d/%m/%Y') if folder.date_arrival else 'N/A'}</td>
                            <td style="padding: 10px; text-align: center; color: #856404; font-weight: bold; border: 1px solid #dee2e6;">{jours_restants}</td>
                            <td style="padding: 10px; border: 1px solid #dee2e6;">{folder.user_id.name or 'Non assigné'}</td>
                            <td style="padding: 10px; border: 1px solid #dee2e6;">{folder.num_brd or 'N/A'}</td>
                        </tr>
                """
            
            html_content += """
                    </tbody>
                </table>
            </div>
            """
        
        # Message si aucune alerte
        if not overdue_folders and not danger_folders:
            html_content += """
            <div style="background-color: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 5px; margin-bottom: 20px;">
                <h3 style="color: #155724; margin-top: 0;">
                    ✅ Aucune alerte
                </h3>
                <p style="color: #155724; margin-bottom: 0;">
                    Excellent ! Tous les dossiers sont à jour. Aucune action immédiate n'est requise.
                </p>
            </div>
            """
        
        html_content += f"""
            <div style="background-color: #f8f9fa; padding: 15px; border-radius: 5px; font-size: 12px; color: #6c757d;">
                <p style="margin: 0;">
                    📧 Ce rapport a été généré automatiquement le {today.strftime('%d/%m/%Y à %H:%M')}.<br>
                    🔄 Prochaine mise à jour prévue demain à la même heure.
                </p>
            </div>
        </div>
        """
        
        # Envoi de l'email
        subject = f"🚨 Rapport d'alertes ETA - {len(overdue_folders)} en retard, {len(danger_folders)} en alerte"
        
        mail_values = {
            'subject': subject,
            'body_html': html_content,
            'email_to': ','.join([user.email for user in all_users]),
            'email_from': self.env.user.email or self.env.company.email,
            'auto_delete': True,
        }
        
        mail = self.env['mail.mail'].create(mail_values)
        try:
            mail.send()
        except Exception as e:
            _logger.warning(f"Erreur lors de l'envoi du rapport d'alertes: {e}")

    def _send_alerte_report_fallback(self, overdue_folders, danger_folders, all_users):
        """Méthode supprimée car plus nécessaire"""
        pass

    name = fields.Char(string='Dossier N°', copy=False, index=True, readonly=True, default=lambda self: _('New'))
    num_ot = fields.Char(string='N° OT')
    number = fields.Integer('code', default=_default_number)
    number_stage = fields.Integer('code residual', default=50)
    date_open = fields.Date("Date de Reception OT")
    date_close = fields.Date("Date de Fermeture")
    date_deadline = fields.Date("Date de Fermeture Estimee")
    # date_departure= fields.Date("ETD")
    transpo_type = fields.Selection([('input', 'Chargement'), ('output', 'Dechargement')],
                                    string="Operation sur le navire", default='')
    date_arrival = fields.Date("ETA", tracking=True)
    date_declaration = fields.Date("Date de declaration", tracking=True)
    date_guce = fields.Date("Date de GUCE", tracking=True)
    date_validate = fields.Date("Ordre de Validation", tracking=True)
    date_RVC = fields.Date("Date AVI", tracking=True)
    date_provisiore = fields.Date("Date Provisoire", tracking=True)
    date_liquidation = fields.Date("Date Liquidation", tracking=True)
    date_depot_pad = fields.Date("Date Depot PAD", tracking=True)
    date_quittance = fields.Date("Date Quittance", tracking=True)
    date_bad = fields.Date("BAD", tracking=True)
    date_bl = fields.Date(" date BL", tracking=True)
    date_sortie = fields.Date("SORTIE", tracking=True)
    alerte = fields.Selection(
        [('open', 'Ouverture'), ('danger', 'Danger'), ('overdue', 'Depasse')],
        "Alerte", compute="compute_alerte_date", store=True, default="open")
    customer_id = fields.Many2one(
        'res.partner',
        string='Client',
        tracking=True
    )

    vendor_id = fields.Many2one(
        'res.partner',
        domain="[('supplier_rank', '>', 0)]",
    string='Fournisseur',
    )
    exportator_id = fields.Many2one(
        'exportator.transit',
        string='Exportateur',
    )
    importator_id = fields.Many2one(
        'import.transit',
        string='Importateur',
    )

    consigne_id = fields.Many2one(
        'consignee.transit',
        string='Consignataire',
    )
    len_task = fields.Integer(
        string='empty task',
    )

    destination_id = fields.Many2one(
        'res.country',
        string='Pays de Destination',
    )
    origin_id = fields.Many2one(
        'res.country',
        string="Pays d'origine",
    )

    port_departure = fields.Many2one(
        'port.transit',
        string="Port d'embarquement",
    )
    incoterm = fields.Many2one(
        'stock.incoterms',
        string="Incoterm",
    )
    port_arrival = fields.Many2one(
        'port.transit',
        string="Port de Debarquement",
    )
    num_voy = fields.Char(
        string='N° Voyage',
    )

    num_di = fields.Char(
        string='N° DI',
    )
    code_camsi = fields.Char(
        string='Code CAMSI',
    )
    goods = fields.Char(
        string='MARCHANDISE',
    )
    num_avi = fields.Char(
        string='N° Guce',
    )
    num_besc = fields.Char(
        string='BESC'
    )
    num_pr = fields.Char(
        string='PR'
    )
    num_rvc = fields.Char(
        string='RVC'
    )
    num_rvc_1 = fields.Char(
        string='2eme RVC'
    )
    num_rvc_2 = fields.Char(
        string='3eme RVC'
    )
    num_rvc_3 = fields.Char(
        string='4eme RVC'
    )
    num_rvc_4 = fields.Char(
        string='5eme RVC'
    )
    num_rvc_5 = fields.Char(
        string='6eme RVC'
    )

    num_quittance = fields.Char(
        string='N° Quittance',
    )
    num_manifeste = fields.Char(
        string='MANIFESTE',
    )
    num_pad = fields.Char(
        string='PAD',
    )
    num_depot_pad = fields.Char(
        string='DEPOT PAD',
    )
    num_liquidation = fields.Char(
        string='N° Declaration',
    )
    vessel = fields.Many2one(
        'vessel.transit',
        string='Navire',
    )
    circuit = fields.Boolean(
        string='Circuit',
    )
    scan = fields.Boolean(
        string='Scanneur',
    )
    visit = fields.Selection([('yes', 'OUI'), ('no', 'NON')], string="Visite")
    type_op = fields.Selection([('in', 'Import'), ('out', 'Export')], string="Type d'operation", default='in')
    transp_op = fields.Selection([('air', 'Air'), ('land', 'Land'), ('sea', 'Ocean')], string="Transport",
                                 default='air')
    type_circuit = fields.Selection([('blue', 'BLEU'), ('yellow', 'JAUNE'), ('green', 'VERT'), ('red', 'ROUGE')],
                                    string="Type Circuit", default='blue')
    type_validated = fields.Selection([('open', ''), ('close', 'Valide')], string="Valide", default='open')
    amount_purchased = fields.Float("Valeur imposable en CFA", tracking=True)
    amount_douane = fields.Float("Droit de douane")
    num_brd = fields.Char("Numero de B/L", track_visibility='onchange')
    debour_ids = fields.One2many('debour.transit', 'transit_id', string="services")

    order_ids = fields.One2many('invoice.transit', 'folder_id', string="Marchandises")
    # order_ids=fields.Many2many('invoice.transit',string="Marchandises")
    currency_id = fields.Many2one('res.currency', string='Currency',
                                  required=True, readonly=True,
                                  default=_default_currency)
    company_id = fields.Many2one('res.company', string='Company', change_default=True,
                                 required=True, readonly=True, default=lambda self: self.env.user.company_id)
    user_id = fields.Many2one('res.users', string='Traite Par', track_visibility='onchange',
                              readonly=True,
                              default=lambda self: self.env.user)
    stages = fields.Selection([('transit', 'Dedouanement'), ('accone', 'Acconage'), ('ship', 'Shipping')],
                              string="Processus", default='transit')

    task_checklist = fields.One2many('task.checklist', 'folder_id', string='Check List')
    checklist_progress = fields.Float(compute='_get_checklist_progress', string='Progress', store=True,
                                      default=0.0)
    max_rate = fields.Integer(string='Maximum rate', default=100)
    stage_id = fields.Many2one('stages.transit', string='Stage', ondelete='restrict', track_visibility='onchange',
                               index=True,
                               default=_get_default_stage_id, group_expand='_read_group_stage_ids', copy=False)
    active = fields.Boolean(
        string="Archived",
        default=True,
        help="If a Transit is set to archived, it is not displayed, but still exists.")
    color = fields.Integer('Color Index')
    # time_quittance = fields.Integer('Duree Moyenne Obtention Quittance', compute="_compute_time_quittance_obtention")
    priority = fields.Selection(AVAILABLE_PRIORITIES, string='Priority', index=True, default=AVAILABLE_PRIORITIES[0][0])
    kanban_state = fields.Selection(
        [('grey', 'No next activity planned'), ('red', 'Next activity late'), ('green', 'Next activity is planned')],
        string='Kanban State', compute='_compute_kanban_state')
    service_count = fields.Integer(string='Service Count', compute='_compute_service_count', readonly=True)
    attachment_files = fields.Many2many(
        'ir.attachment', 'folder_ir_attachments_rel',
        'folder_id', 'attachment_id', 'Attachments')

    total_weighty = fields.Float("Poids Total")
    total_fcl20 = fields.Integer("20'")
    total_fcl40 = fields.Integer("40'")
    total_colis = fields.Integer("Colis")
    uom_total_id = fields.Many2one(
        'uom.uom',
        string='Unite de Mesure',
    )
    regime = fields.Char(string="Regime")
    package_ids = fields.One2many('package.folders', 'transit_id', string="Conteneurs")
    
    # Champs pour la gestion des activités et des utilisateurs
   
    is_scheduled = fields.Boolean('Activité programmée')
    
    # Champs analytiques pour les statistiques
    debit = fields.Monetary(
        compute='_compute_debit_credit_balance', 
        string='Débit',
        currency_field='currency_id'
    )
    credit = fields.Monetary(
        compute='_compute_debit_credit_balance', 
        string='Crédit',
        currency_field='currency_id'
    )
    balance = fields.Monetary(
        compute='_compute_debit_credit_balance', 
        string='Solde',
        currency_field='currency_id'
    )
    line_ids = fields.One2many(
        'account.analytic.line',
        compute='_compute_analytic_lines',
        string="Lignes analytiques",
    )
    
    # Champs pour séparer les revenus et charges
    revenue_line_ids = fields.One2many(
        'account.analytic.line',
        compute='_compute_revenue_expense_lines',
        string="Lignes de revenus",
        help="Lignes analytiques avec montant positif (revenus)"
    )
    expense_line_ids = fields.One2many(
        'account.analytic.line',
        compute='_compute_revenue_expense_lines',
        string="Lignes de charges",
        help="Lignes analytiques avec montant négatif (charges)"
    )
    
    customer_invoice_count = fields.Integer(
        compute='_compute_invoice_counts',
        string="Nombre factures clients"
    )
    vendor_bill_count = fields.Integer(
        compute='_compute_invoice_counts', 
        string="Nombre factures fournisseurs"
    )
    
    def compute_current_user(self):
        """ Vérifie si l'utilisateur connecté est celui assigné à l'activité en cours """
        for record in self:
            record.current_user = (
                record.activity_user_id and 
                record.activity_user_id == self.env.user
            )
            
    
    analytic_id = fields.Many2one(
        'account.analytic.account',
        string='Compte Analytique'
        )
    analytic_distribution = fields.Json(
        'Distribution Analytique',
    )
    analytic_precision = fields.Integer(
        store=False,
        default=lambda self: self.env['decimal.precision'].precision_get("Percentage Analytic"),
    )
    current_user  = fields.Boolean(compute="compute_current_user")
    current_activity_id = fields.Many2one('mail.activity', string="Activité en cours")
  
    
    def write(self, values):
        """ Synchronise le nom du compte analytique avec le nom du dossier """
        result = super(TransitFolder, self).write(values)
        
        # Si le nom du dossier change, synchroniser avec le compte analytique
        if 'name' in values:
            for record in self:
                if record.analytic_distribution:
                    # Récupérer le compte analytique lié
                    account_ids = []
                    for account_ids_str in record.analytic_distribution.keys():
                        account_ids.extend([int(id_) for id_ in account_ids_str.split(',')])
                    
                    if account_ids:
                        analytic_account = record.env['account.analytic.account'].browse(account_ids[0])
                        if analytic_account.exists():
                            analytic_account.write({'name': record.name})
        
        return result
    
    
    def get_next_activities(self):
        """ Récupérer les activités suivantes """
        self.ensure_one()
        next_activity = self.activity_ids
        if len(next_activity) >= 1:
            return next_activity
        else:
            return False 

    @api.model
    def create(self, values):
        """ Création d'un dossier avec distribution analytique automatique """
        # Génération des séquences selon le type de stage
        res = []
        if values['stages'] == 'transit':
            if values.get('name', _('New')) == _('New'):
                values['name'] = self.env['ir.sequence'].next_by_code('transit.invoice') or _('New')
            ot_id = self.env.ref('inov_transit.mail_act_rh_courrier_order').id
            new_id = self.env.ref('inov_transit.mail_act_rh_courrier_folder').id
            res = [ot_id, new_id]
        if values['stages'] == 'accone':
            if values.get('name', _('New')) == _('New'):
                values['name'] = self.env['ir.sequence'].next_by_code('transit.acconage') or _('New')
        if values['stages'] == 'ship':
            if values.get('name', _('New')) == _('New'):
                values['name'] = self.env['ir.sequence'].next_by_code('transit.shipping') or _('New')
        
        # Créer le dossier avec le mixin analytique
        result = super(TransitFolder, self).create(values)
        
        plan = False
        if result.stages == 'transit':
            plan = self.env.ref('inov_account.analytic_plan_transit', raise_if_not_found=False)
        elif result.stages == 'accone':
            plan = self.env.ref('inov_account.analytic_plan_acconnages', raise_if_not_found=False)
        elif result.stages == 'ship':
            plan = self.env.ref('inov_account.analytic_plan_shippings', raise_if_not_found=False)
        
        if plan and result.name:
            analytic_account = self.env['account.analytic.account'].sudo().create({
                'name': result.name,
                'plan_id': plan.id
            })
            
            result.write({
                'analytic_id': analytic_account.id,
                'analytic_distribution': {str(analytic_account.id): 100},
                'len_task': len(self.env['mail.activity.type'].search([('stages', '=', result.stages)]))
            })

        # Créer les activités initiales pour le transit
        for task in res:
            create_vals = {
                'activity_type_id': task,
                'summary': result.env['mail.activity.type'].browse([task]).name,
                'automated': True,
                'note': '',
                'date_deadline': result.compute_deadline_date(result.date_open, 0),
                'res_model_id': result.env['ir.model']._get(result._name).id,
                'res_id': result.id,
                'stages': result.stages
            }
            activity = result.env['mail.activity'].create(create_vals)
            activity.action_feedback()
            
      
        return result
    
    def mark_as_done(self):
        """ Fonction exécutée quand l'utilisateur clique sur 'Marquer comme fait' """
        self.ensure_one()
        current_activity = self.get_next_activities()
        if not current_activity:
            raise ValidationError(_("Aucune activité en cours à valider."))
        # Marquer l'activité comme terminée et programmer la prochaine
        current_activity.action_done_schedule_next()
        if self.stages == 'ship':    
            self.stage_id = self.activity_type_id.stage_id.id
        return True   

    def activity_scheduler(self): 
        for record in self:
            if not record.task_checklist:
                activity_xml = ''
                if record.stages == 'transit':
                    activity_xml = 'inov_transit.mail_act_rh_courrier_order'
                if record.stages == 'accone':
                    activity_xml = 'inov_transit.mail_act_rh_courrier_order'
                if record.stages == 'ship':
                    activity_xml = 'inov_shipping.mail_act_rh_shipping_0'
            else:
                activity_xml = record.activity_type_id.name
                
            record.activity_schedule(
            act_type_xmlid = activity_xml,
            date_deadline = record.compute_deadline_date(record.date_open, 2),
            note='',
            summary = self.env.ref(activity_xml).name,
            user_id = self.env.ref(activity_xml).responsible_id.id)
            record.is_scheduled = True
        return True

    
    def _compute_analytic_lines(self):
        """ Calcule les lignes analytiques liées à ce dossier """
        for record in self:
            if record.analytic_distribution:
                # Rechercher les lignes analytiques liées aux comptes de cette distribution
                account_ids = []
                for account_ids_str in record.analytic_distribution.keys():
                    account_ids.extend([int(id_) for id_ in account_ids_str.split(',')])
                
                lines = self.env['account.analytic.line'].search([
                    ('auto_account_id', 'in', account_ids)
                ])
                record.line_ids = lines
            else:
                record.line_ids = self.env['account.analytic.line']

    def _compute_revenue_expense_lines(self):
        """ Calcule séparément les lignes de revenus et de charges """
        for record in self:
            if record.analytic_distribution:
                # Rechercher les lignes analytiques liées aux comptes de cette distribution
                account_ids = []
                for account_ids_str in record.analytic_distribution.keys():
                    account_ids.extend([int(id_) for id_ in account_ids_str.split(',')])
                
                if account_ids:
                    # Récupérer toutes les lignes analytiques
                    all_lines = self.env['account.analytic.line'].search([
                        ('auto_account_id', 'in', account_ids)
                    ])
                    
                    # Séparer les revenus (montant positif) et charges (montant négatif)
                    revenue_lines = all_lines.filtered(lambda l: l.amount > 0)
                    expense_lines = all_lines.filtered(lambda l: l.amount < 0)
                    
                    record.revenue_line_ids = revenue_lines
                    record.expense_line_ids = expense_lines
                else:
                    record.revenue_line_ids = self.env['account.analytic.line']
                    record.expense_line_ids = self.env['account.analytic.line']
            else:
                record.revenue_line_ids = self.env['account.analytic.line']
                record.expense_line_ids = self.env['account.analytic.line']

    @api.depends('line_ids.amount')
    def _compute_debit_credit_balance(self):
        """ Calcule débit, crédit et solde à partir des lignes analytiques """
        for record in self:
            if record.line_ids:
                debit = sum(line.amount for line in record.line_ids if line.amount > 0)
                credit = abs(sum(line.amount for line in record.line_ids if line.amount < 0))
                record.debit = debit
                record.credit = credit
                record.balance = debit - credit
            else:
                record.debit = 0.0
                record.credit = 0.0
                record.balance = 0.0

    @api.depends('analytic_distribution')
    def _compute_invoice_counts(self):
        """ Calcule le nombre de factures clients et fournisseurs liées via la distribution analytique """
        for record in self:
            customer_count = 0
            vendor_count = 0
            
            # Obtenir les IDs des comptes analytiques de la distribution
            account_ids = []
            if record.analytic_distribution:
                for account_ids_str in record.analytic_distribution.keys():
                    account_ids.extend([int(id_) for id_ in account_ids_str.split(',')])
            
            if account_ids:
                # Compter les factures clients
                query = record.env['account.move.line']._search([('move_id.move_type', 'in', record.env['account.move'].get_sale_types())])
                for account_id in account_ids:
                    query.add_where('analytic_distribution ? %s', [str(account_id)])
                query_string, query_param = query.select('DISTINCT account_move_line.move_id')
                record._cr.execute(query_string, query_param)
                customer_count = len(record._cr.dictfetchall())
                
                # Compter les factures fournisseurs
                query = record.env['account.move.line']._search([('move_id.move_type', 'in', record.env['account.move'].get_purchase_types())])
                for account_id in account_ids:
                    query.add_where('analytic_distribution ? %s', [str(account_id)])
                query_string, query_param = query.select('DISTINCT account_move_line.move_id')
                record._cr.execute(query_string, query_param)
                vendor_count = len(record._cr.dictfetchall())
            
            record.customer_invoice_count = customer_count
            record.vendor_bill_count = vendor_count

    def action_view_analytic_lines(self):
        """ Action pour voir les lignes analytiques """
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("analytic.account_analytic_line_action")
        
        # Obtenir les IDs des comptes analytiques de la distribution
        account_ids = []
        if self.analytic_distribution:
            for account_ids_str in self.analytic_distribution.keys():
                account_ids.extend([int(id_) for id_ in account_ids_str.split(',')])
        
        action['domain'] = [('account_id', 'in', account_ids)]
        action['context'] = {
            'default_account_id': account_ids[0] if account_ids else False,
        }
        return action

    def action_view_revenue_lines(self):
        """ Action pour voir uniquement les lignes de revenus """
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("analytic.account_analytic_line_action")
        
        # Obtenir les IDs des lignes de revenus
        revenue_ids = self.revenue_line_ids.ids
        
        action['domain'] = [('id', 'in', revenue_ids)]
        action['name'] = f'Revenus - {self.name}'
        action['context'] = {
            'default_amount': 0.0,
            'search_default_filter_amount_positive': True,
        }
        return action

    def action_view_expense_lines(self):
        """ Action pour voir uniquement les lignes de charges """
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("analytic.account_analytic_line_action")
        
        # Obtenir les IDs des lignes de charges
        expense_ids = self.expense_line_ids.ids
        
        action['domain'] = [('id', 'in', expense_ids)]
        action['name'] = f'Charges - {self.name}'
        action['context'] = {
            'default_amount': 0.0,
            'search_default_filter_amount_negative': True,
        }
        return action

    def action_view_customer_invoices(self):
        """ Action pour voir les factures clients liées via la distribution analytique """
        self.ensure_one()
        # Obtenir les IDs des comptes analytiques de la distribution
        account_ids = []
        if self.analytic_distribution:
            for account_ids_str in self.analytic_distribution.keys():
                account_ids.extend([int(id_) for id_ in account_ids_str.split(',')])
        
        if not account_ids:
            # Si pas de distribution analytique, retourner une vue vide
            return {
                "type": "ir.actions.act_window",
                "res_model": "account.move",
                "domain": [('id', '=', False)],
                "context": {"create": False, 'default_move_type': 'out_invoice'},
                "name": _("Factures clients"),
                'view_mode': 'tree,form',
            }
        
        # Rechercher les factures clients avec distribution analytique correspondante
        query = self.env['account.move.line']._search([('move_id.move_type', 'in', self.env['account.move'].get_sale_types())])
        for account_id in account_ids:
            query.add_where('analytic_distribution ? %s', [str(account_id)])
        query_string, query_param = query.select('DISTINCT account_move_line.move_id')
        self._cr.execute(query_string, query_param)
        move_ids = [line.get('move_id') for line in self._cr.dictfetchall()]
        
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "domain": [('id', 'in', move_ids)],
            "context": {"create": False, 'default_move_type': 'out_invoice'},
            "name": _("Factures clients"),
            'view_mode': 'tree,form',
        }

    def action_view_vendor_bills(self):
        """ Action pour voir les factures fournisseurs liées via la distribution analytique """
        self.ensure_one()
        # Obtenir les IDs des comptes analytiques de la distribution
        account_ids = []
        if self.analytic_distribution:
            for account_ids_str in self.analytic_distribution.keys():
                account_ids.extend([int(id_) for id_ in account_ids_str.split(',')])
        
        if not account_ids:
            # Si pas de distribution analytique, retourner une vue vide
            return {
                "type": "ir.actions.act_window",
                "res_model": "account.move",
                "domain": [('id', '=', False)],
                "context": {"create": False, 'default_move_type': 'in_invoice'},
                "name": _("Factures fournisseurs"),
                'view_mode': 'tree,form',
            }
        
        # Rechercher les factures fournisseurs avec distribution analytique correspondante
        query = self.env['account.move.line']._search([('move_id.move_type', 'in', self.env['account.move'].get_purchase_types())])
        for account_id in account_ids:
            query.add_where('analytic_distribution ? %s', [str(account_id)])
        query_string, query_param = query.select('DISTINCT account_move_line.move_id')
        self._cr.execute(query_string, query_param)
        move_ids = [line.get('move_id') for line in self._cr.dictfetchall()]
        
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "domain": [('id', 'in', move_ids)],
            "context": {"create": False, 'default_move_type': 'in_invoice'},
            "name": _("Factures fournisseurs"),
            'view_mode': 'tree,form',
        }

    def toggle_active(self):
        """ Bascule l'état actif/archivé du dossier """
        for record in self:
            record.active = not record.active


    # @api.onchange('date_arrival')
    # def on_change_date_arrival(self):
    #     self.ensure_one()
    #     for follow in self.message_follower_ids:
    #         channel = follow.channel_id
    #         channel.message_post_with_view('inov_transit.message_channel_folder_link',
    #                                        values={'self': self, 'origin': self},
    #                                        subtype_id=self.env.ref('mail.mt_comment').id)

    # @api.depends('')

    def _stage_find(self, domain=[]):
        return self.env['stages.transit'].search(domain)

    @api.model
    def _read_group_stage_ids(self, stages, domain, order):
        search_domain = []
        stage_ids = stages._search(search_domain, order=order, access_rights_uid=SUPERUSER_ID)
        return stages.browse(stage_ids)

    def action_view_services(self):
        action = self.env.ref('inov_transit.act_res_partner_2_inov_transit').read()[0]
        action['domain'] = [('transit_id', '=', self.id)]
        action['context'] = {
            'default_courier_id': self.id,
            'search_default_courier_id': [self.id]}
        return action

    @api.model
    def _onchange_stage_id_values(self, stage_id):
        """ returns the new values when stage_id has changed """

        if not stage_id:
            return {}
        stage = self.env['stages.transit'].browse(stage_id)
        if stage and stage.number == 0:
            stage.number = self.number_stage + 1
            self.update({
                'number_stage': stage.number
            })
            return {'number': stage.number}
        else:
            return {'number': stage.number}

    @api.onchange('stage_id')
    def _onchange_stage_id(self):
        self.check_stage_follow()


    def check_stage_follow(self):
        self.ensure_one()
        for record in self:
            if len(record.activity_ids) and record.stage_id.number in [105, 106]:
                raise UserError(_('Veuillez Terminer toutes les taches'))
            if record.checklist_progress == 0 and record.stage_id.number in [105, 106]:
                raise UserError(_('Veuiller Planifier avant de passer a cette Etape.'))
            values = record._onchange_stage_id_values(record.stage_id.id)
            record.update(values)


    def _compute_kanban_state(self):
        today = date.today()
        for lead in self:
            kanban_state = 'grey'
            if lead.activity_date_deadline:
                lead_date = fields.Date.from_string(lead.activity_date_deadline)
                if lead_date >= today:
                    kanban_state = 'green'
                else:
                    kanban_state = 'red'
            lead.kanban_state = kanban_state


    def action_validate_folder(self):
        self.ensure_one()
        for record in self:
            if record.number == 104 and not record.date_arrival:
                raise UserError(_('Vous ne pouvez pas Valider ce Dossier sans inserer l"ETA. '))
            if record.number == 104 and not record.num_besc:
                message_id = self.env['message.wizard.gec'].create(
                    {'message': _("Voulez-vous valider le Dossier %s sans BESC ? ") % (record.name)})
                return {
                    'name': _('Warning'),
                    'type': 'ir.actions.act_window',
                    'view_mode': 'form',
                    'res_model': 'message.wizard.gec',
                    'res_id': message_id.id,
                    'context': {
                        'default_folder_id': record.id,
                    },
                    'target': 'new',
                }
            if record.number == 104 and not record.num_rvc:
                message_id = self.env['message.wizard.gec'].create(
                    {'message': _("Voulez-vous valider le Dossier %s sans BESC ? ") % (record.name)})
                return {
                    'name': _('Warning'),
                    'type': 'ir.actions.act_window',
                    'view_mode': 'form',
                    'res_model': 'message.wizard.gec',
                    'res_id': message_id.id,
                    'context': {
                        'default_folder_id': record.id,
                    },
                    'target': 'new',
                }
            return {
                'name': _('Etape Suivante'),
                'type': 'ir.actions.act_window',
                'view_mode': 'form',
                'res_model': 'stage.transit.wizard',
                'view_id': self.env.ref('inov_transit.wizard_debour_transit_form').id,
                'context': {
                    'default_transit_id': record.id,
                    'default_number': record.number,
                    'default_stage': record.stages,
                },
                'target': 'new',
            }

    # @api.multi
    # def action_archive_button(self):
    #     self.ensure_one()
    #     # if not self.activity_ids:
    #     #    raise UserError('Veuillez termine toutes vos taches!!!!!!!')
    #     document_obj = self.env['muk_dms.directory']
    #     for record in self:
    #         piece_no_archive = self.mapped('attachment_files')
    #         parent_obj = document_obj.search([('name', '=', record.customer_id.name)])
    #         if not parent_obj:
    #             if record.stages == 'transit':
    #                 dms_vals = {
    #                     'name': record.customer_id.name,
    #                     'parent_directory': self.env.ref('inov_transit.lmc_directory_01').id,
    #                     'transit_id': record.id,
    #                 }
    #                 parent_obj = document_obj.create(dms_vals)
    #             if record.stages == 'ship':
    #                 dms_vals = {
    #                     'name': record.name,
    #                     'parent_directory': self.env.ref('inov_transit.lmc_directory_03').id,
    #                     'transit_id': record.id,
    #                 }
    #                 parent_obj = document_obj.create(dms_vals)
    #             if record.stages == 'accone':
    #                 dms_vals = {
    #                     'name': record.name,
    #                     'parent_directory': self.env.ref('inov_transit.lmc_directory_02').id,
    #                     'transit_id': record.id,
    #                 }
    #                 parent_obj = document_obj.create(dms_vals)
    #         if record.customer_id:
    #             parent_obj_directory = document_obj.search(
    #                 [('name', '=', record.name), ('parent_directory', '=', parent_obj.id)])
    #             if not parent_obj_directory:
    #                 dms_vals = {
    #                     'name': record.name,
    #                     'parent_directory': parent_obj.id,
    #                     'transit_id': record.id,
    #                 }
    #                 parent_obj_directory = document_obj.create(dms_vals)
    #         if piece_no_archive:
    #             for attachment in piece_no_archive:
    #                 dms_file_vals = {
    #                     'name': attachment.name,
    #                     'content': attachment.datas,
    #                     'directory': parent_obj.id
    #                 }
    #                 self.env['muk_dms.file'].create(dms_file_vals)
    #         search_domain = [('number', '=', record.number), ('stages', '=', record.stages)]
    #         nws_stage = record._stage_find(search_domain)[0]
    #         values = record._onchange_stage_id_values(nws_stage.id)
    #         record.write({'stage_id': nws_stage.id, 'active': False})
    #         record.update(values)


    def action_create_invoice(self):
        self.ensure_one()
        invoice_obj = self.env['account.move']
        invoice_id = invoice_obj.create({
            'partner_id': self.customer_id.id,
            'transit_id': self.id,
            'service_ids': [(6, 0, self.debour_ids.ids)],
            'origin': self.name,
        })
        action = self.env.ref('account.action_invoice_tree1').read()[0]
        action['views'] = [(self.env.ref('account.move_form').id, 'form')]
        action['res_id'] = invoice_id.id
        invoice_id.message_post_with_view('mail.message_origin_link',
                                          values={'self': invoice_id, 'origin': self},
                                          subtype_id=self.env.ref('mail.mt_note').id)
        return action


    def invoice_line_debour(self, invoice):
        self.ensure_one()
        product = self.env.ref('inov_transit.product_product_debours')
        account = product.property_account_income_id or product.categ_id.property_account_income_categ_id
        if not account:
            raise UserError(
                _('Please define income account for this product: "%s" (id:%d) - or for its category: "%s".') %
                (product.name, product.id, product.categ_id.name))

        fpos = self.regime or self.customer_id.property_account_position_id
        if fpos:
            account = fpos.map_account(account)

        taxes = product.taxes_id
        invoice.update({
            'invoice_line_ids': [(0, 0, {
                'name': product.name,
                'sequence': 10,
                'origin': self.name,
                'account_id': account.id,
                'price_unit': 1.0,
                'quantity': self.amount_transit_debours,
                'uom_id': product.uom_id.id,
                'product_id': product.id or False,
                'invoice_line_tax_ids': [(6, 0, taxes.ids)],
            })]
        })

    def action_create_final_invoice(self):
        """Méthode de base pour la création de facture définitive - à surcharger dans les modules héritiers"""
        self.ensure_one()
        # Cette méthode sera surchargée dans inov_account si ce module est installé
        # Sinon, on retourne une action par défaut
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Information',
                'message': 'La fonctionnalité de facture définitive nécessite le module inov_account.',
                'type': 'warning',
            }
        }


class EtapesTransitFolder(models.Model):
    _name = 'stages.transit'
    _inherit = ['mail.thread']
    _description = 'Etapes de Transit'
    _order = "number asc"

    name = fields.Char("Etapes")
    number = fields.Integer("Numero Etape")
    stages = fields.Selection([('transit', 'Dedouanement'), ('accone', 'Acconage'), ('ship', 'Shipping')],
                              string="Processus")


class PortTransit(models.Model):
    _name = 'port.transit'
    _description = 'Port de Transit'

    name = fields.Char("Nom")


class ExportPortTransit(models.Model):
    _name = 'exportator.transit'
    _description = 'ExPort de Transit'

    name = fields.Char("Nom")


class ImportPortTransit(models.Model):
    _name = 'import.transit'
    _description = 'ImPort de Transit'

    name = fields.Char("Nom")


class ConsignatioTransit(models.Model):
    _name = 'consignee.transit'
    _description = 'Consignataire'

    name = fields.Char("Nom")


class PackageTransit(models.Model):
    _name = 'package.transit'
    _description = 'Package Transit'

    name = fields.Char(string='Name')

class NavireTransit(models.Model):
    _name = 'vessel.transit'
    _description = 'Description du Navire'

    name = fields.Char("Navire")
    active = fields.Boolean(default=True)


# class TransitMuksDms(models.Model):
#     _inherit = 'muk_dms.directory'
#
#     transit_id = fields.Many2one(
#         "folder.transit",
#         "Dossier"
#     )


class PackageFolder(models.Model):
    _name = 'package.folders'

    name = fields.Char("Numero du Conteneur", size=11, )
    package_type_id = fields.Many2one(
        'package.transit',
        string='Type de Conteneur',
    )
    date_receipt = fields.Date("Date Ticket de Livraison")
    date_output = fields.Date("Date de Sortie")
    date_delivered = fields.Date("Date Livraison Client")
    date_remove = fields.Date("Date retrait")
    date_return = fields.Date("Date Retour")
    transit_id = fields.Many2one(
        "folder.transit",
        "Dossier"
    )
