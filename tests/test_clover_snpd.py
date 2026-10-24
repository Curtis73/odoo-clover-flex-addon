# -*- coding: utf-8 -*-
"""
Unit tests for Clover Secure Network Pay Display (SNPD) WebSocket Client.
Tests TCP socket handshake, pairing flow, and sale transaction parsing.
"""

import json
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from services.clover_snpd_client import CloverSnpdClient, CloverSnpdException


class TestCloverSnpdClient(unittest.TestCase):

    def setUp(self):
        self.client = CloverSnpdClient(
            ip_address="192.168.10.95",
            port=12345,
            use_ssl=False,
            auth_token="test_token_123",
            device_serial="C143EQ20230587",
            pos_id="Odoo-POS-1",
            timeout=5.0,
        )

    def test_init_validation(self):
        """Verify client initializes URL correctly and rejects missing IP."""
        self.assertEqual(
            self.client.ws_url,
            "ws://192.168.10.95:12345/remote_pay?authenticationToken=test_token_123"
        )
        
        ssl_client = CloverSnpdClient(ip_address="192.168.1.50", port=12346, use_ssl=True)
        self.assertEqual(ssl_client.ws_url, "wss://192.168.1.50:12346/remote_pay")

        with self.assertRaises(ValueError):
            CloverSnpdClient(ip_address="")

    @patch("socket.socket")
    def test_tcp_connection_success(self, mock_socket_cls):
        """Verify fast TCP reachability check on successful connection."""
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        res = self.client.test_tcp_connection()
        self.assertTrue(res["success"])
        mock_sock.connect.assert_called_once_with(("192.168.10.95", 12345))

    @patch("socket.socket")
    def test_tcp_connection_refused(self, mock_socket_cls):
        """Verify informative error message when port is closed."""
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = ConnectionRefusedError("Connection refused")
        mock_socket_cls.return_value = mock_sock

        res = self.client.test_tcp_connection()
        self.assertFalse(res["success"])
        self.assertIn("refused", res["message"])

    @patch("services.clover_snpd_client.websocket.create_connection")
    @patch.object(CloverSnpdClient, "test_tcp_connection")
    def test_initiate_pairing_success(self, mock_tcp, mock_ws_create):
        """Verify pairing handshake issues auth token when device confirms."""
        mock_tcp.return_value = {"success": True}
        mock_ws = MagicMock()
        mock_ws.recv.return_value = json.dumps({
            "method": "PAIRING_SUCCESS",
            "authToken": "permanent_auth_token_xyz999",
        })
        mock_ws_create.return_value = mock_ws

        res = self.client.initiate_pairing("4589")
        self.assertTrue(res["success"])
        self.assertEqual(res["auth_token"], "permanent_auth_token_xyz999")
        self.assertEqual(self.client.auth_token, "permanent_auth_token_xyz999")

    @patch("services.clover_snpd_client.websocket.create_connection")
    def test_send_sale_request_approved(self, mock_ws_create):
        """Verify sale transaction captures card metadata and approved status."""
        mock_ws = MagicMock()
        mock_ws.recv.return_value = json.dumps({
            "method": "SALE_RESPONSE",
            "result": "SUCCESS",
            "id": "CLV_PAY_12345",
            "cardTransaction": {
                "cardType": "VISA",
                "last4": "4242",
                "authCode": "AUTH_9876",
                "entryType": "CHIP_CONTACTLESS",
            },
        })
        mock_ws_create.return_value = mock_ws

        res = self.client.send_sale_request(
            amount_cents=2500,
            external_payment_id="ext-uuid-1234",
            currency="GBP",
            prompt_tip=False,
            print_receipt=True,
        )

        self.assertEqual(res["result"], "SUCCESS")
        self.assertEqual(res["id"], "CLV_PAY_12345")
        self.assertEqual(res["cardTransaction"]["cardType"], "VISA")
        self.assertEqual(res["cardTransaction"]["last4"], "4242")
        self.assertEqual(res["cardTransaction"]["authCode"], "AUTH_9876")


if __name__ == "__main__":
    unittest.main()
