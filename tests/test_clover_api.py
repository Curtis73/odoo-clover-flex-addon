# -*- coding: utf-8 -*-
"""
Unit tests for Clover Cloud Pay REST API Client.
Verifies /connect/v1 payload structures, PCI-DSS log sanitization, and error resilience.
"""

import unittest
from unittest.mock import MagicMock, patch
import requests

try:
    from ..services.clover_api_client import (
        CloverApiClient,
        CloverApiException,
        sanitize_data_for_logs,
    )
except (ImportError, ValueError):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from services.clover_api_client import (
        CloverApiClient,
        CloverApiException,
        sanitize_data_for_logs,
    )


class TestCloverApiClient(unittest.TestCase):

    def setUp(self):
        self.api_token = "test_clover_bearer_token_1234567890"
        self.device_serial = "C143EQ20230587"
        self.client = CloverApiClient(
            api_token=self.api_token,
            environment="sandbox",
            pos_id="Test-POS-01",
        )

    def test_pci_dss_log_sanitizer(self):
        """Verify that sensitive keys (tokens, PAN, passwords) are masked in logs."""
        sensitive_payload = {
            "amount": 2500,
            "authorization": "Bearer secret_token_xyz_987654321",
            "token": "sensitive_oauth_token_val",
            "card": "4111111111111111",
            "cvv": "123",
            "nested": {
                "password": "supersecretpassword",
                "normal_field": "public_data",
            },
        }

        sanitized = sanitize_data_for_logs(sensitive_payload)

        # Check that authorization and token are redacted
        self.assertIn("[REDACTED]", sanitized["authorization"])
        self.assertNotIn("secret_token_xyz", sanitized["authorization"])
        self.assertIn("[REDACTED]", sanitized["card"])
        self.assertEqual(sanitized["cvv"], "[REDACTED]")
        self.assertIn("[REDACTED]", sanitized["nested"]["password"])
        self.assertEqual(sanitized["nested"]["normal_field"], "public_data")
        self.assertEqual(sanitized["amount"], 2500)

    @patch.object(requests.Session, "request")
    def test_send_payment_request_connect_v1(self, mock_request):
        """Verify payment initiation builds the correct Clover Cloud /connect/v1 payload."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.content = b'{"status": "PENDING"}'
        mock_response.json.return_value = {"status": "PENDING"}
        mock_request.return_value = mock_response

        external_id = "550e8400-e29b-41d4-a716-446655440000"
        res = self.client.send_payment_request(
            device_serial=self.device_serial,
            amount_cents=2450,
            external_payment_id=external_id,
            currency="GBP",
            prompt_tip=True,
            print_receipt=True,
        )

        mock_request.assert_called_once()
        args, kwargs = mock_request.call_args

        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["url"], f"{self.client.base_url}/connect/v1/payments")
        self.assertEqual(kwargs["headers"]["X-Clover-Device-Id"], self.device_serial)
        self.assertEqual(kwargs["headers"]["X-POS-Id"], "Test-POS-01")
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {self.api_token}")
        self.assertIn("Idempotency-Key", kwargs["headers"])

        payload = kwargs["json"]
        self.assertEqual(payload["amount"], 2450)
        self.assertEqual(payload["currency"], "GBP")
        self.assertEqual(payload["externalPaymentId"], external_id)
        self.assertEqual(payload["autoAcceptSignature"], True)
        self.assertEqual(payload["disableTip"], False)
        self.assertEqual(payload["disableReceiptSelection"], False)
        self.assertEqual(res["status"], "PENDING")

    @patch.object(requests.Session, "request")
    def test_check_payment_status_approved(self, mock_request):
        """Verify status querying and parsing of approved transaction metadata."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.content = b'{"result": "SUCCESS"}'
        mock_response.json.return_value = {
            "id": "CLOVER_PAY_9876",
            "result": "SUCCESS",
            "cardTransaction": {
                "cardType": "VISA",
                "last4": "4242",
                "authCode": "AUTH123",
            },
        }
        mock_request.return_value = mock_response

        external_id = "test-uuid-1234"
        res = self.client.get_payment_status(self.device_serial, external_id)

        self.assertEqual(res["result"], "SUCCESS")
        self.assertEqual(res["id"], "CLOVER_PAY_9876")
        self.assertEqual(res["cardTransaction"]["last4"], "4242")

    @patch.object(requests.Session, "request")
    def test_ping_and_device_status(self, mock_request):
        """Verify ping and hardware status queries."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.content = b'{"status": "IDLE"}'
        mock_response.json.return_value = {"status": "IDLE"}
        mock_request.return_value = mock_response

        ping = self.client.ping_device(self.device_serial)
        self.assertEqual(ping["status"], "IDLE")

        status = self.client.get_device_status(self.device_serial)
        self.assertEqual(status["status"], "IDLE")

    @patch.object(requests.Session, "request")
    def test_cancel_active_payment(self, mock_request):
        """Verify cancellation signal to device screen."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.content = b'{"status": "CANCELLED"}'
        mock_response.json.return_value = {"status": "CANCELLED"}
        mock_request.return_value = mock_response

        res = self.client.cancel_active_payment(self.device_serial, "test-uuid")
        self.assertEqual(res["status"], "CANCELLED")

    @patch.object(requests.Session, "request")
    def test_api_error_handling(self, mock_request):
        """Verify proper exception raising on HTTP error status from Clover Cloud."""
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 400
        mock_response.content = b'{"message": "Device offline or busy"}'
        mock_response.json.return_value = {"message": "Device offline or busy"}
        mock_request.return_value = mock_response

        with self.assertRaises(CloverApiException) as ctx:
            self.client.send_payment_request(
                device_serial=self.device_serial,
                amount_cents=1000,
                external_payment_id="err-test",
            )

        self.assertIn("Device offline or busy", str(ctx.exception))
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
