# -*- coding: utf-8 -*-
"""
Clover Cloud Pay Display REST API Client.
Implements the formal Clover REST Pay Display API (/connect/v1) for semi-integrated
payment terminals (Clover Flex, Clover Mini, Clover Station).

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
import uuid
from typing import Any, Dict, Optional, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import urllib3

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
    Supports both /connect/v1 Cloud Pay Display endpoints and /v3/merchants endpoints.
    """

    ENDPOINTS = {
        "sandbox": "https://apisandbox.dev.clover.com",
        "prod_us": "https://api.clover.com",
        "prod_eu": "https://api.eu.clover.com",
    }

    def __init__(
        self,
        api_token: Optional[str] = None,
        merchant_id: Optional[str] = None,
        environment: str = "prod_eu",
        pos_id: str = "Odoo-POS-Terminal",
        custom_base_url: Optional[str] = None,
        timeout: Tuple[float, float] = (5.0, 35.0),
        verify_ssl: bool = True,
    ):
        """
        Initialize the Clover API Client.

        :param api_token: Clover OAuth API Token (Bearer) or local auth token
        :param merchant_id: Optional Merchant ID (mId)
        :param environment: 'sandbox', 'prod_us', or 'prod_eu' (UK/EU)
        :param pos_id: Identifier for the calling POS station
        :param custom_base_url: Optional custom URL override for local LAN / gateways
        :param timeout: Tuple of (connect_timeout, read_timeout) in seconds
        :param verify_ssl: Whether to verify SSL certificates (set False for local LAN HTTPS)
        """
        raw_token = (api_token or "").strip()
        if raw_token.lower().startswith("bearer "):
            raw_token = raw_token[7:].strip()
        self.api_token = raw_token
        self.merchant_id = merchant_id.strip() if merchant_id else ""
        self.environment = environment
        self.pos_id = pos_id
        self.base_url = (
            custom_base_url.rstrip("/")
            if custom_base_url
            else self.ENDPOINTS.get(environment, self.ENDPOINTS["prod_eu"])
        )
        self.timeout = timeout
        self.verify_ssl = verify_ssl

        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

    def _get_headers(self, device_serial: str, with_idempotency: bool = False) -> Dict[str, str]:
        """Generate mandatory Clover headers including target device serial."""
        headers = {
            "X-Clover-Device-Id": device_serial.strip(),
            "X-POS-Id": self.pos_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        if self.merchant_id:
            headers["X-Clover-Merchant-Id"] = self.merchant_id
        if with_idempotency:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        return headers

    def _request(
        self,
        method: str,
        path: str,
        device_serial: str,
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        with_idempotency: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes an authenticated HTTP request to Clover with PCI-safe error handling.
        """
        url = f"{self.base_url}{path}"
        headers = self._get_headers(device_serial, with_idempotency=with_idempotency)

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
                verify=self.verify_ssl,
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
        currency: str = "GBP",
        prompt_tip: bool = False,
        auto_accept: bool = True,
        print_receipt: bool = False,
    ) -> Dict[str, Any]:
        """
        Initiates a sale transaction on the target Clover terminal screen via /connect/v1/payments.

        :param device_serial: Serial number of the Clover Flex/Mini
        :param amount_cents: Amount in currency minor units (e.g. 1050 for £10.50)
        :param external_payment_id: UUID v4 generated by Odoo for idempotency
        :param currency: Currency code (GBP, EUR, USD, etc.)
        :param prompt_tip: Whether to display tip prompt on the terminal screen
        :param auto_accept: Whether signature/payment should be automatically confirmed
        :param print_receipt: Print receipt using the Clover device's built-in printer
        :return: JSON response from Clover Cloud
        """
        if external_payment_id:
            external_payment_id = external_payment_id.replace("-", "")[:32]
        path = "/connect/v1/payments"
        payload = {
            "amount": int(amount_cents),
            "currency": currency.upper(),
            "externalPaymentId": external_payment_id,
            "autoAcceptSignature": auto_accept,
            "autoAcceptPaymentConfirmations": auto_accept,
            "disableTip": not prompt_tip,
            "disableReceiptSelection": not print_receipt,
            "disableDuplicateCheck": False,
        }
        return self._request(
            "POST",
            path,
            device_serial=device_serial,
            payload=payload,
            with_idempotency=True,
        )

    def get_payment_status(
        self,
        device_serial: str,
        external_payment_id: str,
        clover_payment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Polls the status of an in-flight payment using its payment ID or external idempotency key.
        """
        if clover_payment_id:
            path = f"/connect/v1/payments/{clover_payment_id}"
            return self._request("GET", path, device_serial=device_serial)
        else:
            clean_id = external_payment_id.replace("-", "")[:32] if external_payment_id else external_payment_id
            path = f"/connect/v1/payments/external/{clean_id}"
            return self._request("GET", path, device_serial=device_serial)

    def cancel_active_payment(
        self,
        device_serial: str,
        external_payment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Sends a cancellation signal to dismiss the payment screen on the Clover terminal.
        """
        path = "/connect/v1/device/cancel"
        payload = {}
        if external_payment_id:
            payload["externalPaymentId"] = external_payment_id.replace("-", "")[:32]
        return self._request(
            "POST",
            path,
            device_serial=device_serial,
            payload=payload,
            with_idempotency=True,
        )

    def void_payment(
        self,
        device_serial: str,
        clover_payment_id: str,
        reason: str = "USER_CANCEL",
    ) -> Dict[str, Any]:
        """
        Voids a recently completed payment before settlement batch close.
        """
        path = f"/connect/v1/payments/{clover_payment_id}/void"
        payload = {"voidReason": reason}
        return self._request(
            "POST",
            path,
            device_serial=device_serial,
            payload=payload,
            with_idempotency=True,
        )

    def refund_payment(
        self,
        device_serial: str,
        clover_payment_id: str,
        amount_cents: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Executes a partial or full refund against a previously approved payment.
        """
        path = f"/connect/v1/payments/{clover_payment_id}/refund"
        payload = {}
        if amount_cents:
            payload["amount"] = int(amount_cents)
        return self._request(
            "POST",
            path,
            device_serial=device_serial,
            payload=payload,
            with_idempotency=True,
        )

    def ping_device(self, device_serial: str) -> Dict[str, Any]:
        """
        Sends a live ping to the Cloud Pay Display app on the physical Clover device.
        """
        path = "/connect/v1/device/ping"
        return self._request(
            "POST",
            path,
            device_serial=device_serial,
            payload={},
            with_idempotency=True,
        )

    def get_device_status(self, device_serial: str) -> Dict[str, Any]:
        """
        Queries the current device hardware state (e.g. IDLE, BUSY, WAITING_FOR_CARD).
        """
        path = "/connect/v1/device/status"
        return self._request("GET", path, device_serial=device_serial)

    def test_connection(self, device_serial: str) -> Dict[str, Any]:
        """
        Comprehensive diagnostic: verifies Clover Cloud connectivity and target device responsiveness.
        """
        try:
            ping_res = self.ping_device(device_serial)
            status_res = self.get_device_status(device_serial)
            device_state = status_res.get("status") or status_res.get("state") or "READY"

            return {
                "success": True,
                "message": f"Successfully connected to Clover device {device_serial}. Terminal state: {device_state}",
                "ping": ping_res,
                "status": status_res,
            }
        except CloverApiException as exc:
            return {
                "success": False,
                "message": str(exc),
            }
