# This file is part shipment_invoice_report_from_shipment module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.pool import Pool
from . import stock

def register():
    Pool.register(
        stock.Move,
        stock.ShipmentOut,
        stock.ShipmentOutReturn,
        module='shipment_invoice_report_from_shipment', type_='model')
    Pool.register(
        module='shipment_invoice_report_from_shipment', type_='wizard')
    Pool.register(
        stock.ShipmentOutInvoiceReport,
        stock.ShipmentOutReturnInvoiceReport,
        module='shipment_invoice_report_from_shipment', type_='report')
