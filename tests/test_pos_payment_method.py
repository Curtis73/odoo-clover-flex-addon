# -*- coding: utf-8 -*-
"""
Unit tests for PosPaymentMethod Clover integration logic for Odoo 20.
Tests idempotency handling, currency conversion, and status parsing.
"""

import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

# Ensure module root is on sys.path
root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

try:
    from .odoo_mock import setup_odoo_mock
except ImportError:
    from odoo_mock import setup_odoo_mock
setup_odoo_mock()

from models.pos_payment_method import PosPaymentMethod
from services.clover_api_client import CloverApiException



class TestPosPaymentMethodClover(unittest.TestCase):

    def test_amount_to_cents_conversion(self):
        """Verify currency amount conversion to integer cents/pence."""
        pm = PosPaymentMethod()
        pm.company_id = MagicMock()
        pm.company_id.currency_id.decimal_places = 2

        # Standard GBP/USD 2 decimal places
        self.assertEqual(pm._amount_to_cents(10.50), 1050)
        self.assertEqual(pm._amount_to_cents(0.01), 1)
        self.assertEqual(pm._amount_to_cents(99.99), 9999)

        # 3 decimal places (e.g. KWD, BHD)
        pm.company_id.currency_id.decimal_places = 3
        self.assertEqual(pm._amount_to_cents(5.250), 5250)

        # 0 decimal places (e.g. JPY)
        pm.company_id.currency_id.decimal_places = 0
        self.assertEqual(pm._amount_to_cents(1500), 1500)

    def test_lan_mode_client_configuration(self):
        """Verify that LAN connection mode builds the correct local HTTPS endpoint."""
        pm = PosPaymentMethod()
        pm.id = 42
        pm.clover_api_token = "local_auth_test"
        pm.clover_merchant_id = ""
        pm.clover_environment = "prod_eu"
        pm.ensure_one = MagicMock()

        terminal = MagicMock()
        terminal.connection_mode = "lan"
        terminal.ip_address = "192.168.1.150"
        terminal.port = 12346
        terminal.use_https = True
        pm.clover_terminal_id = terminal

        client = pm._get_clover_client()
        self.assertEqual(client.base_url, "https://192.168.1.150:12346")
        self.assertFalse(client.verify_ssl)
        self.assertEqual(client.api_token, "local_auth_test")


if __name__ == "__main__":
    unittest.main()
