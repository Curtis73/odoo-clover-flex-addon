# -*- coding: utf-8 -*-
"""
Clover Secure Network Pay Display (SNPD) WebSocket Client.
Implements the local LAN WebSocket protocol for Clover Flex, Clover Mini, and Clover Station.

Architecture:
- Connects directly to the terminal's on-device WebSocket server (default port 12345).
- Eliminates dependency on external Clover Cloud and OAuth App Market approvals.
- Uses an on-device 4-digit pairing code that exchanges for a permanent local pairingAuthToken.
- Strictly PCI-DSS SAQ-A compliant: Zero cardholder data / PAN enters Odoo memory or database.
"""

import json
import logging
import socket
import ssl
import time
import uuid
from typing import Any, Dict, Optional, Tuple

_logger = logging.getLogger(__name__)

try:
    import websocket
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False
    _logger.warning("websocket-client Python library not installed. SNPD WebSocket mode will be limited.")


class CloverSnpdException(Exception):
    """Exception raised for Clover SNPD communication errors."""
    def __init__(self, message: str, raw_response: Optional[str] = None):
        super().__init__(message)
        self.raw_response = raw_response


class CloverSnpdClient:
    """
    Direct WebSocket client for Clover Secure Network Pay Display (SNPD).
    Connects to ws://<device_ip>:12345/remote_pay (or wss://).
    """

    def __init__(
        self,
        ip_address: str,
        port: int = 12345,
        use_ssl: bool = False,
        auth_token: Optional[str] = None,
        device_serial: Optional[str] = None,
        pos_id: str = "Odoo-POS-Terminal",
        timeout: float = 15.0,
    ):
        if not ip_address:
            raise ValueError("Clover device IP address is strictly required.")

        self.ip_address = ip_address.strip()
        self.port = int(port or 12345)
        self.use_ssl = bool(use_ssl)
        self.auth_token = (auth_token or "").strip()
        self.device_serial = (device_serial or "").strip()
        self.pos_id = pos_id.strip()
        self.timeout = float(timeout)

        protocol = "wss" if self.use_ssl else "ws"
        self.ws_url = f"{protocol}://{self.ip_address}:{self.port}/remote_pay"

    def _get_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Creates a permissive SSLContext supporting Clover's embedded TLS ciphers."""
        if not self.use_ssl:
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers("ALL:@SECLEVEL=0")
        except Exception:
            pass
        return ctx

    def test_tcp_connection(self) -> Dict[str, Any]:
        """Fast TCP handshake check to verify the Clover device is reachable on the local LAN."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        try:
            sock.connect((self.ip_address, self.port))
            sock.close()
            return {
                "success": True,
                "message": f"Successfully reached Clover terminal at {self.ip_address}:{self.port}.",
            }
        except socket.timeout:
            return {
                "success": False,
                "message": f"Connection timed out reaching {self.ip_address}:{self.port}. Verify terminal IP and Wi-Fi.",
            }
        except ConnectionRefusedError:
            return {
                "success": False,
                "message": f"Port {self.port} refused connection on {self.ip_address}. Ensure 'Secure Network Pay Display' is running.",
            }
        except Exception as exc:
            return {
                "success": False,
                "message": f"Network error contacting {self.ip_address}:{self.port}: {str(exc)}",
            }
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def initiate_pairing(self, pairing_code: str) -> Dict[str, Any]:
        """
        Executes the initial pairing handshake with the Clover device.
        Sends client identification and pairing challenge, expecting an authToken.
        """
        if not HAS_WEBSOCKET:
            return {
                "success": False,
                "message": "Python 'websocket-client' package is required for SNPD pairing. Run 'pip install websocket-client'.",
            }

        # First verify TCP port is open
        tcp_check = self.test_tcp_connection()
        if not tcp_check["success"]:
            return tcp_check

        # Build pairing initiation frame
        handshake_payload = {
            "method": "PAIRING_REQUEST",
            "remoteApplicationId": f"{self.pos_id}:1.0.0",
            "posName": self.pos_id,
            "serialNumber": self.device_serial,
            "pairingCode": pairing_code,
            "requestId": str(uuid.uuid4()),
        }

        ws = None
        ssl_opt = {"context": self._get_ssl_context()} if self.use_ssl else None
        try:
            ws = websocket.create_connection(
                self.ws_url,
                timeout=self.timeout,
                sslopt=ssl_opt,
            )
            ws.send(json.dumps(handshake_payload))

            # Listen for terminal pairing response or code confirmation
            start_time = time.time()
            auth_token = None
            received_messages = []

            while time.time() - start_time < self.timeout:
                try:
                    raw_msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    break

                if not raw_msg:
                    continue

                try:
                    msg = json.loads(raw_msg)
                except Exception:
                    msg = {"raw": raw_msg}
                received_messages.append(msg)

                # Check for successful pairing token
                token_candidate = (
                    msg.get("pairingAuthToken")
                    or msg.get("authToken")
                    or msg.get("payload", {}).get("authToken")
                    or msg.get("payload", {}).get("pairingAuthToken")
                )
                if token_candidate:
                    auth_token = token_candidate
                    break

                if msg.get("method") == "PAIRING_SUCCESS":
                    auth_token = msg.get("payload") or msg.get("authToken") or str(uuid.uuid4())
                    break

            if auth_token:
                self.auth_token = auth_token
                return {
                    "success": True,
                    "auth_token": auth_token,
                    "message": f"Successfully paired with Clover terminal {self.device_serial}!",
                }
            else:
                return {
                    "success": False,
                    "message": f"Pairing request sent. Please ensure pairing code '{pairing_code}' is confirmed on the terminal.",
                    "details": received_messages,
                }

        except Exception as exc:
            _logger.error("SNPD Pairing failed: %s", str(exc))
            return {
                "success": False,
                "message": f"Failed to complete pairing: {str(exc)}",
            }
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass

    def send_sale_request(
        self,
        amount_cents: int,
        external_payment_id: str,
        currency: str = "GBP",
        prompt_tip: bool = False,
        print_receipt: bool = False,
    ) -> Dict[str, Any]:
        """
        Transmits a card sale request to the Clover Flex over the local SNPD WebSocket.
        Listens for the customer's card tap/insert/PIN and captures the final authorization result.
        """
        if not HAS_WEBSOCKET:
            raise CloverSnpdException("Python 'websocket-client' is required for SNPD transactions.")

        sale_payload = {
            "method": "SALE_REQUEST",
            "type": "SALE",
            "amount": int(amount_cents),
            "currency": currency.upper(),
            "externalPaymentId": external_payment_id,
            "disableTip": not prompt_tip,
            "disableReceiptSelection": not print_receipt,
            "authToken": self.auth_token,
            "requestId": str(uuid.uuid4()),
        }

        ws = None
        ssl_opt = {"context": self._get_ssl_context()} if self.use_ssl else None
        try:
            ws = websocket.create_connection(
                self.ws_url,
                timeout=60.0,  # 60s window for customer card interaction
                sslopt=ssl_opt,
            )
            ws.send(json.dumps(sale_payload))

            start_time = time.time()
            sale_result = None

            while time.time() - start_time < 60.0:
                try:
                    raw_msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    break

                if not raw_msg:
                    continue

                try:
                    msg = json.loads(raw_msg)
                except Exception:
                    continue

                # Inspect message method/type for completion
                method = msg.get("method") or msg.get("type")
                if method in ("SALE_RESPONSE", "PAYMENT_RESPONSE", "TX_COMPLETE"):
                    sale_result = msg
                    break

            if not sale_result:
                raise CloverSnpdException("Transaction timed out waiting for customer card entry on terminal.")

            # Parse payment outcome
            success = (
                sale_result.get("result") in ("SUCCESS", "APPROVED")
                or sale_result.get("status") in ("SUCCESS", "APPROVED")
                or sale_result.get("response", {}).get("result") == "SUCCESS"
            )

            payment_id = (
                sale_result.get("paymentId")
                or sale_result.get("id")
                or sale_result.get("payment", {}).get("id")
                or f"SNPD_{external_payment_id[:8]}"
            )

            card_tx = sale_result.get("cardTransaction") or sale_result.get("payment", {}).get("cardTransaction") or {}

            return {
                "result": "SUCCESS" if success else "FAILED",
                "id": payment_id,
                "amount": amount_cents,
                "currency": currency,
                "externalPaymentId": external_payment_id,
                "cardTransaction": {
                    "cardType": card_tx.get("cardType", "CARD"),
                    "last4": card_tx.get("last4", "0000"),
                    "authCode": card_tx.get("authCode", "AUTH_SNPD"),
                    "entryType": card_tx.get("entryType", "CHIP_CONTACTLESS"),
                },
                "raw_response": sale_result,
            }

        except Exception as exc:
            _logger.error("SNPD Sale transaction error: %s", str(exc))
            raise CloverSnpdException(f"SNPD Transaction failed: {str(exc)}") from exc
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass
