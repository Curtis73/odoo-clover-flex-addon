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

    @patch("services.clover_snpd_client.CloverSnpdClient.send_sale_request")
    def test_snpd_initiate_payment(self, mock_sale):
        """Verify SNPD mode dispatches to CloverSnpdClient and captures card details."""
        mock_sale.return_value = {
            "result": "SUCCESS",
            "id": "SNPD_PAY_888",
            "cardTransaction": {
                "cardType": "MASTERCARD",
                "last4": "5678",
                "authCode": "APP_1234",
            },
        }

        pm_model = PosPaymentMethod()
        pm_record = MagicMock()
        pm_record.id = 1
        pm_record.exists.return_value = True
        pm_record.use_payment_terminal = "clover_cloud"
        pm_record.clover_allow_tip = False
        pm_record.clover_auto_print_receipt = True
        pm_record._amount_to_cents.return_value = 1000

        terminal = MagicMock()
        terminal.id = 5
        terminal.name = "Counter Flex"
        terminal.device_serial = "C143EQ20230587"
        terminal.connection_mode = "snpd"
        terminal.ip_address = "192.168.10.95"
        terminal.port = 12345
        terminal.use_https = False
        terminal.pairing_auth_token = "valid_pairing_token_abc"
        pm_record.clover_terminal_id = terminal

        mock_tx = MagicMock()
        mock_tx_model = MagicMock()
        mock_tx_model.search.return_value = None
        mock_tx_model.create.return_value = mock_tx

        pm_model.browse = MagicMock(return_value=pm_record)
        pm_model.env = {
            "clover.transaction": MagicMock(sudo=MagicMock(return_value=mock_tx_model)),
            "company": MagicMock(currency_id=MagicMock(name="GBP")),
        }

        res = pm_model.action_clover_initiate_payment(1, 10.0, "ext-tx-uuid-1")
        self.assertTrue(res["success"])
        self.assertEqual(res["state"], "success")
        self.assertEqual(res["clover_payment_id"], "SNPD_PAY_888")
        self.assertEqual(res["card_brand"], "MASTERCARD")
        self.assertEqual(res["card_last4"], "5678")
        mock_tx.write.assert_called_once()

    def test_payment_provider_field_sync(self):
        """Verify synchronization between payment_provider and use_payment_terminal."""
        pm = PosPaymentMethod()
        vals_list = [{"name": "Clover Card", "payment_provider": "clover_cloud"}]
        with patch.object(PosPaymentMethod, "create", wraps=pm.create) if hasattr(PosPaymentMethod, "create") else patch("unittest.mock.MagicMock"):
            pass
        # Test onchange methods
        pm.use_payment_terminal = "clover_cloud"
        pm.payment_provider = None
        pm._onchange_use_payment_terminal()
        self.assertEqual(pm.payment_provider, "clover_cloud")

        pm.payment_provider = "clover_cloud"
        pm.use_payment_terminal = None
        pm._onchange_payment_provider()
        self.assertEqual(pm.use_payment_terminal, "clover_cloud")


if __name__ == "__main__":
    unittest.main()
