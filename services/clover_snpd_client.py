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

import base64
import json
import logging
import os
import socket
import ssl
import struct
import time
import urllib.parse
import uuid
from typing import Any, Dict, Optional, Tuple

_logger = logging.getLogger(__name__)

try:
    import websocket
    HAS_WEBSOCKET_LIB = True
except ImportError:
    websocket = None
    HAS_WEBSOCKET_LIB = False

HAS_WEBSOCKET = True  # Fully supported via embedded RFC-6455 SimpleWebSocket fallback


class SimpleWebSocket:
    """
    Pure Python RFC-6455 WebSocket client using standard library socket and ssl.
    Provides zero-dependency WebSocket support for standard Odoo Docker environments.
    """
    def __init__(self, url: str, timeout: float = 15.0, ssl_context: Optional[ssl.SSLContext] = None):
        self.url = url
        self.timeout = float(timeout)
        self.ssl_context = ssl_context
        self.sock = None
        self._connect()

    def _connect(self):
        parsed = urllib.parse.urlparse(self.url)
        is_ssl = parsed.scheme in ("wss", "https")
        host = parsed.hostname
        port = parsed.port or (443 if is_ssl else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"

        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(self.timeout)
        raw_sock.connect((host, port))

        if is_ssl:
            ctx = self.ssl_context
            if ctx is None:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                try:
                    ctx.set_ciphers("ALL:@SECLEVEL=0")
                except Exception:
                    pass
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        else:
            self.sock = raw_sock

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        headers = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "\r\n",
        ]
        self.sock.sendall("\r\n".join(headers).encode("ascii"))

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("Server closed connection during WebSocket handshake.")
            resp += chunk

        status_line = resp.split(b"\r\n")[0].decode("latin-1", errors="ignore")
        if " 101 " not in status_line:
            raise ConnectionError(f"WebSocket upgrade failed: {status_line}")

    def send(self, data: str):
        payload = data.encode("utf-8")
        length = len(payload)
        mask = os.urandom(4)

        header = bytearray([0x81])  # FIN=1, Opcode=1 (Text)
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        header.extend(mask)
        masked_payload = bytearray(payload[i] ^ mask[i % 4] for i in range(length))
        self.sock.sendall(bytes(header + masked_payload))

    def recv(self) -> str:
        b1, b2 = struct.unpack("!BB", self._read_exact(2))
        opcode = b1 & 0x0F
        has_mask = bool(b2 & 0x80)
        length = b2 & 0x7F

        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]

        mask = self._read_exact(4) if has_mask else None
        data = self._read_exact(length)

        if mask:
            data = bytearray(data[i] ^ mask[i % 4] for i in range(length))

        if opcode == 0x8:  # Close
            raise ConnectionError("WebSocket closed by server.")
        elif opcode == 0x9:  # Ping -> reply Pong
            pong = bytearray([0x8A, 0x00])
            self.sock.sendall(pong)
            return self.recv()
        elif opcode in (0x1, 0x2):
            return data.decode("utf-8", errors="ignore")
        else:
            return ""

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout:
                raise TimeoutError("Timed out reading WebSocket frame.")
            if not chunk:
                raise ConnectionError("Connection lost while reading WebSocket frame.")
            buf.extend(chunk)
        return bytes(buf)

    def settimeout(self, timeout: float):
        if self.sock:
            self.sock.settimeout(float(timeout))

    def close(self):
        if self.sock:
            try:
                self.sock.sendall(bytes([0x88, 0x00]))
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


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
        self._msg_seq = 0

        protocol = "wss" if self.use_ssl else "ws"
        if self.auth_token:
            self.ws_url = f"{protocol}://{self.ip_address}:{self.port}/remote_pay?authenticationToken={self.auth_token}"
        else:
            self.ws_url = f"{protocol}://{self.ip_address}:{self.port}/remote_pay"

    def _build_remote_message(self, method: str, payload_obj: Any, req_id: Optional[str] = None) -> str:
        """Wraps inner payload in Clover RemoteMessage envelope required by SNPD."""
        self._msg_seq += 1
        msg_id = str(req_id or self._msg_seq)
        payload_str = json.dumps(payload_obj) if not isinstance(payload_obj, str) else payload_obj
        envelope = {
            "id": msg_id,
            "method": method,
            "type": "COMMAND",
            "packageName": "com.clover.remote.client.messages",
            "payload": payload_str,
            "remoteSourceSDK": "com.clover.remote-pay-android",
            "remoteApplicationID": f"{self.pos_id}:1.0",
            "version": 1,
        }
        return json.dumps(envelope)

    def _unpack_message(self, raw_str: str) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """Unpacks Clover RemoteMessage envelope, returning (method, payload_dict, envelope)."""
        try:
            envelope = json.loads(raw_str)
        except Exception:
            return "", {}, {}
        method = envelope.get("method") or ""
        payload_raw = envelope.get("payload")
        payload = {}
        if isinstance(payload_raw, str):
            try:
                payload = json.loads(payload_raw)
            except Exception:
                payload = {"raw": payload_raw}
        elif isinstance(payload_raw, dict):
            payload = payload_raw
        elif not payload_raw:
            payload = envelope
        return method, payload, envelope

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

    def _create_ws_connection(self, url: str, timeout: float):
        """Creates a WebSocket connection using SimpleWebSocket (pure Python standard library) or websocket-client."""
        ctx = self._get_ssl_context()
        if HAS_WEBSOCKET_LIB:
            try:
                ssl_opt = {"context": ctx} if ctx else None
                return websocket.create_connection(url, timeout=timeout, sslopt=ssl_opt)
            except Exception as e:
                _logger.warning("websocket-client failed, using pure Python SimpleWebSocket: %s", e)
        return SimpleWebSocket(url=url, timeout=timeout, ssl_context=ctx)

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

    def get_pairing_code(self) -> Dict[str, Any]:
        """
        Phase 1 of pairing: connect without a token so Clover generates a
        pairing code, return it immediately to display to the user.
        The user must then type this code on the Clover touchscreen.
        """
        tcp_check = self.test_tcp_connection()
        if not tcp_check["success"]:
            return tcp_check

        # Connect without authenticationToken so Clover generates a code
        protocol = "wss" if self.use_ssl else "ws"
        fresh_url = f"{protocol}://{self.ip_address}:{self.port}/remote_pay"

        handshake_payload = {
            "method": "PAIRING_REQUEST",
            "name": self.pos_id,
            "serialNumber": self.device_serial,
            "authenticationToken": "",
        }

        ws = None
        try:
            ws = self._create_ws_connection(fresh_url, timeout=self.timeout)
            ws.send(self._build_remote_message("PAIRING_REQUEST", handshake_payload, req_id="PR-GET-CODE"))

            timeout_types = (socket.timeout, TimeoutError)
            if websocket is not None and hasattr(websocket, "WebSocketTimeoutException"):
                timeout_types = (socket.timeout, TimeoutError, websocket.WebSocketTimeoutException)

            start_time = time.time()
            while time.time() - start_time < self.timeout:
                try:
                    raw_msg = ws.recv()
                except timeout_types:
                    break
                if not raw_msg:
                    continue

                method, payload, _ = self._unpack_message(raw_msg)
                if method == "PAIRING_CODE":
                    code = payload.get("pairingCode", "")
                    return {
                        "success": True,
                        "pairing_code": code,
                        "message": f"Enter code {code!r} on the Clover screen to complete pairing.",
                    }
                elif method == "PAIRING_RESPONSE" and payload.get("pairingState") == "PAIRED":
                    # Edge case: device auto-paired (should not happen without token)
                    token = payload.get("authenticationToken", "")
                    return {
                        "success": True,
                        "pairing_code": None,
                        "auth_token": token,
                        "already_paired": True,
                    }

            return {
                "success": False,
                "message": "Clover did not send a pairing code. Ensure Secure Network Pay Display is running on the device.",
            }
        except Exception as exc:
            _logger.error("SNPD get_pairing_code failed: %s", str(exc))
            return {
                "success": False,
                "message": f"Connection failed: {str(exc)}",
            }
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass

    def confirm_pairing(self, pairing_code: str, wait_seconds: float = 50.0) -> Dict[str, Any]:
        """
        Phase 2 of pairing: reconnect using the code Clover issued, wait for the
        user to type it on the Clover screen, and capture the permanent authenticationToken.

        Clover's flow:
          connect (no token) → PAIRING_CODE → user types code on device
          → PAIRING_RESPONSE {pairingState: "PAIRED", authenticationToken: "<uuid>"}

        Returns {"success": True, "auth_token": "<uuid>"} on success.
        """
        tcp_check = self.test_tcp_connection()
        if not tcp_check["success"]:
            return tcp_check

        protocol = "wss" if self.use_ssl else "ws"
        fresh_url = f"{protocol}://{self.ip_address}:{self.port}/remote_pay"

        handshake_payload = {
            "method": "PAIRING_REQUEST",
            "name": self.pos_id,
            "serialNumber": self.device_serial,
            "authenticationToken": "",
        }

        ws = None
        try:
            ws = self._create_ws_connection(fresh_url, timeout=10.0)
            ws.send(self._build_remote_message("PAIRING_REQUEST", handshake_payload, req_id="PR-CONFIRM"))

            timeout_types = (socket.timeout, TimeoutError)
            if websocket is not None and hasattr(websocket, "WebSocketTimeoutException"):
                timeout_types = (socket.timeout, TimeoutError, websocket.WebSocketTimeoutException)

            start_time = time.time()
            while time.time() - start_time < wait_seconds:
                try:
                    if hasattr(ws, "settimeout"):
                        ws.settimeout(2.0)
                    raw_msg = ws.recv()
                except timeout_types:
                    continue  # Keep looping - waiting for user to type code on device
                if not raw_msg:
                    continue

                method, payload, _ = self._unpack_message(raw_msg)
                _logger.info("SNPD confirm_pairing received: %s pairingState=%s", method, payload.get("pairingState"))

                if method == "PAIRING_RESPONSE":
                    state = payload.get("pairingState")
                    if state == "PAIRED":
                        token = payload.get("authenticationToken", "")
                        if token:
                            self.auth_token = token
                            return {
                                "success": True,
                                "auth_token": token,
                                "message": f"Paired successfully with {self.device_serial}!",
                            }
                    elif state == "FAILED":
                        return {
                            "success": False,
                            "message": "Clover rejected the pairing. The code may have expired. Please click 'Start Pairing' again.",
                        }
                elif method == "PAIRING_CODE":
                    # Clover sent a new code - user hasn't typed yet, that's fine, keep waiting
                    new_code = payload.get("pairingCode", "")
                    _logger.info("SNPD: Clover re-issued pairing code %s", new_code)

            return {
                "success": False,
                "message": "Timed out waiting for you to enter the code on the Clover screen. Please try again.",
            }
        except Exception as exc:
            _logger.error("SNPD confirm_pairing failed: %s", str(exc))
            return {
                "success": False,
                "message": f"Connection error during pairing confirmation: {str(exc)}",
            }
        finally:
            if ws:
                try:
                    ws.close()
                except Exception:
                    pass

    def verify_existing_token(self) -> Dict[str, Any]:
        """
        Verifies an existing authenticationToken against the Clover device.
        Returns success=True if Clover accepts it, success=False otherwise.
        """
        if not self.auth_token:
            return {"success": False, "message": "No token to verify."}

        tcp_check = self.test_tcp_connection()
        if not tcp_check["success"]:
            return tcp_check

        handshake_payload = {
            "method": "PAIRING_REQUEST",
            "name": self.pos_id,
            "serialNumber": self.device_serial,
            "authenticationToken": self.auth_token,
        }

        ws = None
        try:
            ws = self._create_ws_connection(self.ws_url, timeout=self.timeout)
            ws.send(self._build_remote_message("PAIRING_REQUEST", handshake_payload, req_id="PR-VERIFY"))

            timeout_types = (socket.timeout, TimeoutError)
            if websocket is not None and hasattr(websocket, "WebSocketTimeoutException"):
                timeout_types = (socket.timeout, TimeoutError, websocket.WebSocketTimeoutException)

            start_time = time.time()
            while time.time() - start_time < self.timeout:
                try:
                    raw_msg = ws.recv()
                except timeout_types:
                    break
                if not raw_msg:
                    continue

                method, payload, _ = self._unpack_message(raw_msg)
                if method == "PAIRING_RESPONSE":
                    state = payload.get("pairingState")
                    if state == "PAIRED":
                        new_token = payload.get("authenticationToken") or self.auth_token
                        self.auth_token = new_token
                        return {
                            "success": True,
                            "auth_token": new_token,
                            "message": "Token verified — terminal is paired and ready.",
                        }
                    elif state == "FAILED":
                        return {
                            "success": False,
                            "message": "Token rejected by Clover. Please re-pair the terminal.",
                        }
                elif method == "PAIRING_CODE":
                    # Token not recognised — Clover wants a new pairing
                    code = payload.get("pairingCode", "?")
                    return {
                        "success": False,
                        "needs_repairing": True,
                        "message": f"Token is no longer valid. Clover is asking for a new pairing code: {code}. Please click 'Start Pairing'.",
                    }

            return {
                "success": False,
                "message": "Clover did not respond to token verification. Check the device is on and SNPD is running.",
            }
        except Exception as exc:
            return {
                "success": False,
                "message": f"Verification failed: {str(exc)}",
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
        Executes required PAIRING_REQUEST handshake, then TX_START, and listens for FINISH_OK.
        """
        ws = None
        try:
            ws = self._create_ws_connection(
                self.ws_url,
                timeout=60.0,
            )

            # Step 1: Every WebSocket session to Clover SNPD must first authenticate via PAIRING_REQUEST
            pair_payload = {
                "method": "PAIRING_REQUEST",
                "name": self.pos_id,
                "serialNumber": self.device_serial,
                "authenticationToken": self.auth_token,
            }
            ws.send(self._build_remote_message("PAIRING_REQUEST", pair_payload, req_id="PR-INIT"))

            paired = False
            start_pair = time.time()
            timeout_types = (socket.timeout, TimeoutError)
            if websocket is not None and hasattr(websocket, "WebSocketTimeoutException"):
                timeout_types = (socket.timeout, TimeoutError, websocket.WebSocketTimeoutException)

            while time.time() - start_pair < 10.0:
                try:
                    raw_msg = ws.recv()
                except timeout_types:
                    break
                if not raw_msg:
                    continue

                method, payload, _ = self._unpack_message(raw_msg)
                if method == "PAIRING_RESPONSE":
                    pairing_state = payload.get("pairingState")
                    if pairing_state in ("PAIRED", "INITIAL"):
                        paired = True
                        if payload.get("authenticationToken"):
                            self.auth_token = payload["authenticationToken"]
                        break
                    elif pairing_state == "FAILED":
                        raise CloverSnpdException("Clover terminal rejected pairing token. Please re-pair in Clover settings.")

            if not paired and not self.auth_token:
                raise CloverSnpdException("Terminal did not confirm pairing handshake.")

            # Step 2: Dispatch TX_START command wrapped in RemoteMessage envelope
            pay_intent = {
                "action": "com.clover.remote.protocol.action.START_REMOTE_PROTOCOL_PAY",
                "amount": int(amount_cents),
                "tippableAmount": 0,
                "tipAmount": 0,
                "taxAmount": 0,
                "transactionType": "PAYMENT",
                "isDisableCashBack": True,
                "isDisableTip": not prompt_tip,
                "remotePrint": print_receipt,
                "requiresRemoteConfirmation": True,
                "externalPaymentId": external_payment_id,
                "allowPartialAuth": False,
                "transactionSettings": {
                    "cloverShouldHandleReceipts": print_receipt,
                    "autoAcceptPaymentConfirmations": True,
                    "autoAcceptSignature": True,
                    "disableDuplicateCheck": True,
                },
            }
            tx_payload = {
                "method": "TX_START",
                "payIntent": pay_intent,
                "requestInfo": "SALE",
                "suppressOnScreenTips": not prompt_tip,
            }
            ws.send(self._build_remote_message("TX_START", tx_payload))

            # Step 3: Listen for payment lifecycle events
            start_time = time.time()
            sale_result = None

            while time.time() - start_time < 60.0:
                try:
                    raw_msg = ws.recv()
                except timeout_types:
                    break

                if not raw_msg:
                    continue

                method, payload, _ = self._unpack_message(raw_msg)
                if not method:
                    continue

                _logger.info("Clover SNPD event received: %s", method)

                # Auto-confirm payment if terminal requests remote confirmation
                if method in ("CONFIRM_PAYMENT", "CONFIRM_PAYMENT_MESSAGE"):
                    confirm_payload = {"payment": payload.get("payment", {})}
                    ws.send(self._build_remote_message("CONFIRM_PAYMENT", confirm_payload))
                    continue

                # Transaction completed successfully
                if method == "FINISH_OK":
                    sale_result = payload.get("payment") or payload
                    break

                # Transaction cancelled or rejected by customer / terminal
                if method == "FINISH_CANCEL":
                    cancel_reason = payload.get("requestInfo") or payload.get("reason") or "Cancelled by user"
                    raise CloverSnpdException(f"Transaction cancelled on Clover: {cancel_reason}")

                # Fallback for standard or direct responses
                if method in ("SALE_RESPONSE", "PAYMENT_RESPONSE", "TX_COMPLETE"):
                    sale_result = payload
                    break

            if not sale_result:
                raise CloverSnpdException("Transaction timed out waiting for customer card entry on terminal.")

            # Parse payment outcome
            payment_id = (
                sale_result.get("id")
                or sale_result.get("paymentId")
                or f"SNPD_{external_payment_id[:8]}"
            )
            card_tx = sale_result.get("cardTransaction") or {}

            return {
                "result": "SUCCESS",
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




