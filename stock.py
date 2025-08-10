# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import datetime
from trytond import backend
from trytond.model import fields, ModelView
from trytond.pool import Pool, PoolMeta
from trytond.exceptions import UserError, UserWarning
from trytond.transaction import Transaction
from trytond.report import Report
from trytond.rpc import RPC
from trytond.pyson import Eval, Bool
from trytond.i18n import gettext
from sql import Literal


class Move(metaclass=PoolMeta):
    __name__ = 'stock.move'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        # Override core behaviour by making quantity read-write when state is
        # assigned
        cls.quantity.states['readonly'] &= Eval('state') != 'assigned'
        cls._deny_modify_assigned.discard('quantity')

    @classmethod
    def write(cls, *args):
        pool = Pool()
        Warning = pool.get('res.user.warning')
        actions = iter(args)
        to_warn = []
        for moves, values in zip(actions, actions):
            if not 'quantity' in values:
                continue
            for move in moves:
                if move.state != 'assigned':
                    continue
                if (values['quantity'] or 0) > move.quantity:
                    to_warn.append((move, values['quantity']))
        if to_warn:
            key = Warning.format('quantity_greater_than_assigned',
                [x[0] for x in to_warn])
            if Warning.check(key):
                message = []
                for move, quantity in to_warn:
                    quantity = move.unit.round(quantity)
                    message.append(f'{move.product.rec_name}: '
                        f'{move.quantity} -> {quantity}')
                raise UserWarning(key, gettext(
                    'shipment_invoice_report_from_shipment.msg_quantity_greater_than_assigned',
                    moves='\n'.join(message)))
        super().write(*args)


class ShipmentOut(metaclass=PoolMeta):
    __name__ = 'stock.shipment.out'

    user = fields.Function(fields.Many2One('res.user', 'User'), 'get_user',
        searcher='search_user')
    invoices = fields.Function(fields.One2Many('account.invoice', None,
        'Invoices'), 'get_invoices')
    processing = fields.Function(fields.Boolean('Processing'), 'get_processing',
        searcher='search_processing')
    printed_on = fields.DateTime('Printed', readonly=True)
    postable = fields.Function(fields.Boolean('Postable'), 'get_postable')

    @classmethod
    def __register__(cls, module_name):
        exists = backend.TableHandler.table_exist(cls._table)
        table_h = cls.__table_handler__(module_name)
        created_printed_on = exists and not table_h.column_exist('printed_on')
        super().__register__(module_name)
        if created_printed_on:
            cursor = Transaction().connection.cursor()
            table = cls.__table__()
            # Set printed_on = now() for all shipments in 'done' state
            cursor.execute(*table.update(
                    [table.printed_on],
                    [Literal(datetime.datetime.now())],
                    where=(table.state == 'done') & (table.printed_on == None)))
    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
                'post_invoices': {
                    'invisible': ~Bool(Eval('postable')),
                    'depends': ['postable'],
                    },
                'pick_pack_do': {
                    'invisible': ~Eval('state').in_(['assigned', 'picked',
                            'packed']),
                    'depends': ['state'],
                    },
                })
        cls._buttons['pick']['invisible'] = True
        cls._buttons['pack']['invisible'] = True
        cls._buttons['do']['invisible'] = True

    def get_user(self, name):
        return self.write_uid or self.create_uid

    @classmethod
    def search_user(cls, name, clause):
        return ['OR',
            [('write_uid',) + tuple(clause[1:])
            ], [
            ('create_uid',) + tuple(clause[1:]),
            ('write_uid', '=', None),
            ]]

    def get_invoices(self, name):
        invoices = set()
        for move in self.moves:
            for line in move.invoice_lines:
                if line.invoice is not None:
                    invoices.add(line.invoice.id)
        return list(invoices)

    def get_processing(self, name):
        if self.state != 'done' or self.printed_on:
            return False
        if all([x.state in ('posted', 'paid') for x in self.invoices]):
            return False
        return True

    @classmethod
    def search_processing(cls, name, clause):
        shipments = cls.search([
                ('state', '=', 'done'),
                ('printed_on', '=', None),
                ])
        # It's not very efficient but the number of shipments in this
        # state should be small
        res = []
        for shipment in shipments:
            if shipment.processing:
                res.append(shipment.id)
        op = 'in'
        if clause[1] == '=' and not clause[2]:
            op = 'not in'
        elif clause[1] == '!=' and clause[2]:
            op = 'not in'
        return [('id', op, res)]

    @classmethod
    @ModelView.button
    def post_invoices(cls, shipments):
        pool = Pool()
        Invoice = pool.get('account.invoice')
        Sale = pool.get('sale.sale')
        SaleLine = pool.get('sale.line')

        to_process = []
        for shipment in shipments:
            to_process += [x.origin.sale for x in shipment.outgoing_moves if
                 isinstance(x.origin, SaleLine)]
        to_process = Sale.browse(list(set(to_process)))
        Sale.process(to_process)
        to_post = []
        for shipment in cls.browse(shipments):
            if not shipment.postable:
                continue
            for invoice in shipment.invoices:
                if invoice.state == 'draft' and invoice not in to_post:
                    to_post.append(invoice)
        to_post = Invoice.browse(list(set(to_post)))
        Invoice.post(to_post)

    def get_postable(self, name):
        if self.state != 'done':
            return False
        method = getattr(self.customer, 'sale_invoice_grouping_method',
            None)
        if method == 'standard':
            period = getattr(self.customer,
                'sale_invoice_grouping_period', None)
            if not period or period != 'daily':
                return False
        if any([x for x in self.invoices if x.state == 'draft']):
            return True
        return False

    def print_invoice(cls, shipments):
        pass

    @classmethod
    @ModelView.button
    def pick_pack_do(cls, shipments):
        cls.pick(shipments)
        cls.pack(shipments)
        cls.do(shipments)
        to_save = []
        for shipment in shipments:
            cls.__queue__.post_invoices([shipment])
            # If sale is set to manual invoice method, discard the shipment
            # from the list of shipments to print by setting it as printed
            sales = {x.sale for x in shipment.outgoing_moves if x.sale}
            if all([x.invoice_method == 'manual' for x in sales]):
                shipment.printed_on = datetime.datetime.now()
                to_save.append(shipment)
        cls.save(to_save)


class ShipmentOutInvoiceReport(Report):
    'Shipment Out Invoice Report'
    __name__ = 'stock.shipment.out.invoice'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        # As invoice report is stored if it does not exist, we need the
        # transaction to be read-write
        cls.__rpc__['execute'] = RPC(False, check_access=False)

    @classmethod
    def execute(cls, ids, data):
        cls.check_access()
        pool = Pool()
        Shipment = pool.get('stock.shipment.out')
        InvoiceReport = Pool().get('account.invoice', type='report')

        if not ids:
            return
        shipments = Shipment.browse(ids)
        # Sort shipments by customer and number before printing
        # This better suits users needs because they'll put all invoices
        # from the same customer in the same envelope
        shipments = Shipment.search([
                ('id', 'in', ids),
                ], order=[('customer.name', 'ASC'), ('number', 'ASC')])
        invoice_ids = [x.id for y in shipments for x in y.invoices]
        if not invoice_ids:
            raise UserError(gettext('shipment_invoice_report_from_shipment.msg_no_invoice_to_print',
                    shipment=shipments[0].rec_name))
        #Pass and empty dict instead of data, to prevent super from getting the
        #incorrect invoice action (ShipmentOutInvoiceReport instead of InvoiceReport)
        now = datetime.datetime.now()
        for shipment in shipments:
            shipment.printed_on = now
        Shipment.save(shipments)
        # Pass and empty dict instead of data, to prevent super from getting
        # the incorrect invoice action (ShipmentOutInvoiceReport instead of
        # InvoiceReport)
        return InvoiceReport.execute(invoice_ids, {})


class ShipmentOutReturn(metaclass=PoolMeta):
    __name__ = 'stock.shipment.out.return'

    user = fields.Function(fields.Many2One('res.user', 'User'), 'get_user',
        searcher='search_user')
    invoices = fields.Function(fields.One2Many('account.invoice', None, 'Invoices'),
        'get_invoices')
    postable = fields.Function(fields.Boolean('Postable'), 'get_postable')

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._buttons.update({
                'post_invoices': {
                    'invisible': ~Bool(Eval('postable')),
                    'depends': ['postable'],
                    },
                })

    def get_user(self, name):
        return self.write_uid or self.create_uid

    @classmethod
    def search_user(cls, name, clause):
        return ['OR',
            [('write_uid',) + tuple(clause[1:])
            ], [
            ('create_uid',) + tuple(clause[1:]),
            ('write_uid', '=', None),
            ]]

    def get_invoices(self, name):
        invoices = []
        for move in self.moves:
            for line in move.invoice_lines:
                if line.invoice not in invoices:
                    invoices.append(line.invoice)
        return invoices

    @classmethod
    @ModelView.button
    def post_invoices(cls, shipments):
        pool = Pool()
        Invoice = pool.get('account.invoice')

        to_post = []
        for shipment in shipments:
            if not shipment.postable:
                continue
            for invoice in shipment.invoices:
                if invoice.state == 'draft' and invoice not in to_post:
                    to_post.append(invoice)
        Invoice.post(to_post)

    def get_postable(self, name):
        if self.state != 'done':
            return False
        method = getattr(self.customer, 'sale_invoice_grouping_method',
            None)
        if method == 'standard':
            period = getattr(self.customer,
                'sale_invoice_grouping_period', None)
            if not period or period != 'daily':
                return False
        if any([x for x in self.invoices if x.state == 'draft']):
            return True
        return False

    @classmethod
    @ModelView.button_action('report_shipment_out_return_invoice')
    def print_invoice(cls, shipments):
        pass


class ShipmentOutReturnInvoiceReport(Report):
    'Shipment Out Return Invoice Report'
    __name__ = 'stock.shipment.out.return.invoice'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.__rpc__['execute'] = RPC(False, check_access=False)

    @classmethod
    def execute(cls, ids, data):
        pool = Pool()
        Shipment = pool.get('stock.shipment.out.return')

        if not ids:
            return
        InvoiceReport = Pool().get('account.invoice', type='report')

        shipments = Shipment.browse(ids)
        invoice_ids = [x.id for y in shipments for x in y.invoices]
        if not invoice_ids:
            raise UserError(gettext(
                'shipment_invoice_report_from_shipment.msg_no_invoice_to_print',
                shipment=shipments[0].rec_name))
        # Pass and empty dict instead of data, to prevent super from getting
        # the incorrect invoice action (ShipmentOutReturnInvoiceReport instead
        # of InvoiceReport)
        return InvoiceReport.execute(invoice_ids, {})
