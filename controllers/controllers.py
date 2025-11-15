# -*- coding: utf-8 -*-
# from odoo import http


# class TransitInvoice(http.Controller):
#     @http.route('/inov_transit/inov_transit', auth='public')
#     def index(self, **kw):
#         return "Hello, world"

#     @http.route('/inov_transit/inov_transit/objects', auth='public')
#     def list(self, **kw):
#         return http.request.render('inov_transit.listing', {
#             'root': '/inov_transit/inov_transit',
#             'objects': http.request.env['inov_transit.inov_transit'].search([]),
#         })

#     @http.route('/inov_transit/inov_transit/objects/<model("inov_transit.inov_transit"):obj>', auth='public')
#     def object(self, obj, **kw):
#         return http.request.render('inov_transit.object', {
#             'object': obj
#         })

