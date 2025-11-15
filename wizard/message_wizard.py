# -*- coding: utf-8 -*-

from odoo import models, fields, api, _

class PopupWizard(models.TransientModel):
    _name='message.wizard.gec'
    message=fields.Text('Messages',required=True, readonly=True)
    folder_id = fields.Many2one(
        'folder.transit',
        string='folder',
        )


    def action_ok(self):
        """ close wizard"""
        folder_id=self.env.context.get('default_folder_id')
        folder=self.env['folder.transit'].browse([folder_id])
        if not folder.num_besc:
            search_domain=[('number','=',103)]
            nws_stage=folder._stage_find(search_domain)
            values = folder._onchange_stage_id_values(nws_stage.id)
            folder.write({'stage_id': nws_stage.id})
            folder.update(values)
        if not folder.num_rvc:
            nws_stage=self.env.ref('inov_transit.stages_variable_validate_rvc')
            values = folder._onchange_stage_id_values(nws_stage.id)
            folder.write({'stage_id': nws_stage.id})
            folder.update(values)
        return {'type': 'ir.actions.act_window_close'}