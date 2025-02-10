import datetime as dt
import unittest
from trytond.tests.test_tryton import drop_db
from decimal import Decimal
from proteus import Model
from trytond.modules.account.tests.tools import (create_chart,
                                                 create_fiscalyear,
                                                 get_accounts)
from trytond.modules.account_invoice.tests.tools import (
    create_payment_term, set_fiscalyear_invoice_sequences)
from trytond.tests.tools import activate_modules, set_user
from trytond.modules.company.tests.tools import create_company, get_company
from trytond.exceptions import UserWarning

class Test(unittest.TestCase):
    def setUp(self):
        drop_db()
        super().setUp()

    def tearDown(self):
        drop_db()
        super().tearDown()

    def test_unit(self):
        today = dt.date.today()

        # Activate modules
        config = activate_modules(['shipment_invoice_report_from_shipment', 'sale'])

        # Create company
        _ = create_company()
        company = get_company()

        # Create sale user
        User = Model.get('res.user')
        Group = Model.get('res.group')
        sale_user = User()
        sale_user.name = 'Sale'
        sale_user.login = 'sale'
        sale_group, = Group.find([('name', '=', 'Sales')])
        sale_user.groups.append(sale_group)
        stock_group, = Group.find([('name', '=', 'Stock')])
        sale_user.groups.append(stock_group)
        sale_user.save()

        # Create account user
        account_user = User()
        account_user.name = 'Account'
        account_user.login = 'account'
        account_user.save()

        # Create fiscal year
        fiscalyear = set_fiscalyear_invoice_sequences(
            create_fiscalyear(company))
        fiscalyear.click('create_period')

        # Create chart of accounts
        _ = create_chart(company)
        accounts = get_accounts(company)
        revenue = accounts['revenue']
        expense = accounts['expense']

        # Create parties
        Party = Model.get('party.party')
        supplier = Party(name='Supplier')
        supplier.save()

        customer = Party(name='Customer')
        customer.save()

        # Create account category
        ProductCategory = Model.get('product.category')
        account_category = ProductCategory(name="Account Category")
        account_category.accounting = True
        account_category.account_expense = expense
        account_category.account_revenue = revenue
        account_category.save()

        # Create product
        ProductUom = Model.get('product.uom')
        unit, = ProductUom.find([('name', '=', 'Unit')])
        ProductTemplate = Model.get('product.template')
        template = ProductTemplate()
        template.name = 'product'
        template.default_uom = unit
        template.type = 'goods'
        template.salable = True
        template.list_price = Decimal('10')
        template.account_category = account_category
        template.save()
        product, = template.products

        # Create payment term
        payment_term = create_payment_term()
        payment_term.save()

        # Get warehouse
        Location = Model.get('stock.location')
        warehouse, = Location.find([('code', '=', 'WH')])
        supplier_location, = Location.find([('code', '=', 'SUP')])

        # Create stock move for the product with 5 units at unit price 10
        StockMove = Model.get('stock.move')

        # Skipping warning
        stock_move = StockMove()
        stock_move.product = product

        stock_move.from_location = supplier_location
        stock_move.to_location = warehouse.storage_location
        stock_move.quantity = 5
        stock_move.currency = company.currency
        stock_move.unit_price = Decimal('10')

        # Skip warning because the move has no origin
        config.skip_warning = True
        stock_move.click('do')
        config.skip_warning = False

        # Create product sale and moves
        set_user(sale_user)
        Sale = Model.get('sale.sale')
        SaleLine = Model.get('sale.line')
        sale = Sale()
        sale.party = customer
        sale.payment_term = payment_term
        sale.invoice_method = 'shipment'
        sale.save()
        sale_line = SaleLine()
        sale_line.sale = sale
        sale_line.product = product
        sale_line.quantity = 5
        sale_line.unit_price = Decimal('10')
        sale_line.save()
        sale.click('quote')
        config.skip_warning = True
        sale.click('confirm')
        config.skip_warning = False

        shipment, = sale.shipments
        shipment.planned_date = today
        shipment.effective_date = today
        shipment.customer = customer
        shipment.warehouse = warehouse
        shipment.company = company
        shipment.click('assign_try')
        shipment.reload()
        outgoing_move, = shipment.outgoing_moves
        self.assertEqual(outgoing_move.quantity, 5)

        inventory_move, = sorted(shipment.inventory_moves,
            key=lambda x: x.quantity, reverse=True)
        self.assertEqual(inventory_move.state, 'assigned')
        self.assertEqual(inventory_move.quantity, 5)

        # Ensure we can change the quantity even in assigned state
        with self.assertRaises(UserWarning):
            inventory_move.quantity = 6
            inventory_move.save()
        inventory_move.quantity = 4
        inventory_move.save()
        shipment.click('pick_pack_do')

        # Check invoices
        self.assertEqual(len(sale.invoices), 1)
        invoice, = sale.invoices
        self.assertEqual(invoice.state, 'posted')
        self.assertEqual(invoice, shipment.invoices[0])
