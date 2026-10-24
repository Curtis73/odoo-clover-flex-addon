# -*- coding: utf-8 -*-
"""
Clover Cloud Pay Display REST API Client.
Implements the formal Clover REST Pay Display API (v3) for semi-integrated payment terminals
(Clover Flex, Clover Mini, Clover Station).

PCI-DSS COMPLIANCE NOTICE:
--------------------------
1. This client strictly interfaces with Clover's semi-integrated Cloud Pay architecture.
2. The POS system NEVER touches, handles, or stores Primary Account Numbers (PAN), CVVs,
   or Sensitive Authentication Data (SAD).
3. All cardholder interactions and PIN entries occur exclusively on the physical Clover hardware.
4. Logging is rigorously sanitized to prevent any accidental leakage of credentials or sensitive data.
"""

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_logger = logging.getLogger(__name__)

# Sanitization patterns to ensure PCI-DSS log safety
SENSITIVE_KEY_PATTERNS = re.compile(
    r"(authorization|token|secret|password|card|pan|cvv|pin|track)",
    re.IGNORECASE
)


def sanitize_data_for_logs(data: Any) -> Any:
    """
    Recursively sanitize dictionaries/lists for logging to enforce PCI-DSS compliance.
    Masks API tokens, authorization headers, and any potential cardholder metadata.
    """
    if isinstance(data, dict):
        sanitized = {}
        for key, value in data.items():
            if SENSITIVE_KEY_PATTERNS.search(str(key)):
                if isinstance(value, str) and len(value) > 8:
                    sanitized[key] = f"{value[:4]}...[REDACTED]...{value[-4:]}"
                else:
                    sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize_data_for_logs(value)
        return sanitized
    elif isinstance(data, list):
        return [sanitize_data_for_logs(item) for item in data]
    return data


class CloverApiException(Exception):
    """Base exception for Clover API failures."""
    def __init__(self, message: str, status_code: Optional[int] = None, raw_response: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.raw_response = raw_response


class CloverApiClient:
    """
    Formal Client for Clover Cloud REST Pay Display API.
    Interacts with Clover Cloud Proxy to send commands to physical Clover Flex and Mini terminals.
    """

    ENDPOINTS = {
        "sandbox": "https://apisandbox.dev.clover.com",
        "prod_us": "https://api.clover.com",
        "prod_eu": "https://api.eu.clover.com",
    }

    def __init__(
        self,
        api_token: str,
        merchant_id: str,
        environment: str = "prod_eu",
        custom_base_url: Optional[str] = None,
        timeout: Tuple[float, float] = (5.0, 30.0),
    ):
        """
        Initialize the Clover API Client.

        :param api_token: Clover OAuth API Token (Bearer)
        :param merchant_id: Merchant ID (mId)
        :param environment: 'sandbox', 'prod_us', or 'prod_eu' (UK/EU)
        :param custom_base_url: Optional custom URL override for testing/gateways
        :param timeout: Tuple of (connect_timeout, read_timeout) in seconds
        """
        if not api_token or not merchant_id:
            raise ValueError("Clover API token and Merchant ID are strictly required.")

        self.api_token = api_token.strip()
        self.merchant_id = merchant_id.strip()
        self.environment = environment
        self.base_url = (
            custom_base_url.rstrip("/")
            if custom_base_url
            else self.ENDPOINTS.get(environment, self.ENDPOINTS["prod_eu"])
        )
        self.timeout = timeout

        # Configure high-performance session with connection pooling
        self.session = requests.Session()
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"],  # Only retry idempotent GET queries automatically
        )
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=retry_strategy,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _get_headers(self, device_serial: str) -> Dict[str, str]:
        """Generate mandatory Clover Cloud headers including target device serial."""
        return {
            "Authorization": f"Bearer {self.api_token}",
            "X-Clover-Device-Id": device_serial.strip(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        device_serial: str,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Executes an authenticated HTTP request to Clover Cloud with PCI-safe error handling.
        """
        url = f"{self.base_url}{path}"
        headers = self._get_headers(device_serial)

        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug(
                "Clover API Request: %s %s [Device: %s] Payload: %s",
                method,
                url,
                device_serial,
                json.dumps(sanitize_data_for_logs(payload or {})),
            )

        try:
            response = self.session.request(
                method=method,
                url=url,
                headers=headers,
                json=payload,
                params=params,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout as exc:
            _logger.error("Clover API timeout contacting %s: %s", url, str(exc))
            raise CloverApiException(
                f"Communication timeout contacting Clover Cloud for device {device_serial}."
            ) from exc
        except requests.exceptions.RequestException as exc:
            _logger.error("Clover API connection error: %s", str(exc))
            raise CloverApiException(
                f"Network error connecting to Clover Cloud: {str(exc)}"
            ) from exc

        # Parse response body
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {"raw_text": response.text}

        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug(
                "Clover API Response [%d]: %s",
                response.status_code,
                json.dumps(sanitize_data_for_logs(body)),
            )

        if not response.ok:
            error_msg = body.get("message") or body.get("error") or response.reason
            _logger.warning(
                "Clover API call failed [%d]: %s",
                response.status_code,
                error_msg,
            )
            raise CloverApiException(
                message=f"Clover error ({response.status_code}): {error_msg}",
                status_code=response.status_code,
                raw_response=response.text,
            )

        return body

    def send_payment_request(
        self,
        device_serial: str,
        amount_cents: int,
        external_payment_id: str,
        prompt_tip: bool = False,
        auto_accept: bool = True,
        print_receipt: bool = False,
    ) -> Dict[str, Any]:
        """
        Initiates a sale transaction on the target Clover terminal screen.

        :param device_serial: Serial number of the Clover Flex/Mini
        :param amount_cents: Amount in currency minor units (e.g. 1050 for £10.50)
        :param external_payment_id: UUID v4 generated by Odoo for idempotency
        :param prompt_tip: Whether to display tip prompt on the terminal screen
        :param auto_accept: Whether signature/payment should be automatically confirmed
        :param print_receipt: Print receipt using the Clover device's built-in printer
        :return: JSON response from Clover Cloud
        """
        path = f"/v3/merchants/{self.merchant_id}/pay"
        payload = {
            "amount": int(amount_cents),
            "externalPaymentId": external_payment_id,
            "autoAcceptPayment": auto_accept,
            "autoAcceptSignature": True,
            "disableTip": not prompt_tip,
            "disableReceiptSelection": not print_receipt,
            "disableDuplicateCheck": False,
        }
        return self._request("POST", path, device_serial=device_serial, payload=payload)

    def get_payment_status(
        self,
        device_serial: str,
        external_payment_id: str,
    ) -> Dict[str, Any]:
        """
        Polls the status of an in-flight payment using its unique external idempotency key.

        :param device_serial: Serial number of the Clover Flex/Mini
        :param external_payment_id: UUID v4 of the transaction
        :return: JSON payment status payload
        """
        path = f"/v3/merchants/{self.merchant_id}/pay/{external_payment_id}"
        return self._request("GET", path, device_serial=device_serial)

    def cancel_active_payment(
        self,
        device_serial: str,
        external_payment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Sends a cancellation signal to dismiss the payment screen on the Clover terminal.
        """
        path = f"/v3/merchants/{self.merchant_id}/device/cancel"
        payload = {}
        if external_payment_id:
            payload["externalPaymentId"] = external_payment_id
        return self._request("POST", path, device_serial=device_serial, payload=payload)

    def refund_payment(
        self,
        device_serial: str,
        clover_payment_id: str,
        amount_cents: int,
        external_refund_id: str,
    ) -> Dict[str, Any]:
        """
        Executes a partial or full refund against a previously approved payment.

        :param device_serial: Target device serial
        :param clover_payment_id: The ID of the original approved payment in Clover
        :param amount_cents: Refund amount in cents
        :param external_refund_id: UUID v4 for the refund idempotency
        """
        path = f"/v3/merchants/{self.merchant_id}/payments/{clover_payment_id}/refund"
        payload = {
            "amount": int(amount_cents),
            "externalReferenceId": external_refund_id,
        }
        return self._request("POST", path, device_serial=device_serial, payload=payload)

    def test_connection(self, device_serial: str) -> Dict[str, Any]:
        """
        Verifies connectivity with the Clover Cloud and validates that the device
        serial is registered to this merchant.
        """
        path = f"/v3/merchants/{self.merchant_id}/devices"
        devices_data = self._request("GET", path, device_serial=device_serial)
        elements = devices_data.get("elements", [])
        
        matched_device = None
        for dev in elements:
            if dev.get("serial") == device_serial:
                matched_device = dev
                break

        if not matched_device:
            # Check if elements has any device or if direct query succeeded
            return {
                "success": False,
                "message": f"Device serial '{device_serial}' was not found among registered merchant devices.",
                "devices_found": [d.get("serial") for d in elements],
            }

        return {
            "success": True,
            "message": f"Successfully connected to Clover Cloud. Device '{matched_device.get('model', 'Clover')}' is registered.",
            "device": matched_device,
        }
