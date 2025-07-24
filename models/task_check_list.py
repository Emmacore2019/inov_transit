# -*- coding: utf-8 -*-

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import html2plaintext, clean_context


class TaskChecklist(models.Model):
    _inherit = 'mail.activity.type'

    stages = fields.Selection([('transit', 'Dedouanement'), ('accone', 'Acconage'), ('ship', 'Shipping')],
                              string="Processus")
    responsible_id = fields.Many2one(
        'res.users',
        string='Poste',
    )
    stage_id = fields.Many2one(
        'stages.transit',
        string='Etapes',
        )


class TaskActivityTransit(models.Model):
    _inherit = 'mail.activity'

    model_transit_id = fields.Many2one(
        'folder.transit',
        string='Dossier',
        compute='compute_folder_transit_field',
    )

    stages = fields.Selection([('transit', 'Dedouanement'), ('accone', 'Acconage'), ('ship', 'Shipping')],
                              string="Processus")


    @api.depends('res_id')
    def compute_folder_transit_field(self):
        for record in self:
            if record.res_id:
                record.model_transit_id = self.env['folder.transit'].browse(record.res_id).id

    
    
    def action_feedback(self, feedback=False, attachment_ids=None):
        for activity in self:
            vals = {
                    'name': activity.activity_type_id.name,
                    'responsible_id': activity.user_id.name,
                    'date_start': activity.date_deadline,
                    'date_dealine': fields.Date.today(),
                    'folder_id': activity.res_id,
                }
            self.env['task.checklist'].create(vals)
        messages, _next_activities = self.with_context(
            clean_context(self.env.context)
        )._action_done(feedback=feedback, attachment_ids=attachment_ids)
        return messages[0].id if messages else False
       
class TaskTransitChecklist(models.Model):
    _name = 'task.checklist'
    _description = 'Checklist for the task'

    name = fields.Char(string='Name', required=True)
    responsible_id = fields.Char(
        string='Poste',
    )
    description = fields.Char("Observation")
    date_dealine = fields.Date("Date Validation", required=True)
    date_start = fields.Date("Date Creation de la tache", required=True)
    folder_id = fields.Many2one("folder.transit", string="Dossier")
    
    time_spent_days = fields.Integer(
        string="Temps passé (jours)",
        compute="_compute_time_spent",
        store=True
    )
    time_spent_float = fields.Float(
        string="Temps passé (heures décimales)",
        compute="_compute_time_spent",
        store=True
    )
    
    state = fields.Selection([('transit', 'Dedouanement'), ('accone', 'Acconage'), ('ship', 'Shipping')], string='Type de dossier', related="folder_id.stages",store=True)
    
    @api.depends('date_start', 'date_dealine')
    def _compute_time_spent(self):
        for rec in self:
            if rec.date_start and rec.date_dealine:
                delta = rec.date_dealine - rec.date_start
                rec.time_spent_days = delta.days
                rec.time_spent_float = delta.seconds
            else:
                rec.time_spent_days = 0
                rec.time_spent_float = 0.0


