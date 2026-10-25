# -*- coding: utf-8 -*-
import logging
import threading
import time
import urllib.parse
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import odoo

try:
    from ..services.clover_api_client import CloverApiClient, CloverApiException
    from ..services.clover_snpd_client import CloverSnpdClient, CloverSnpdException
except (ImportError, ValueError):
    from services.clover_api_client import CloverApiClient, CloverApiException
    from services.clover_snpd_client import CloverSnpdClient, CloverSnpdException

_logger = logging.getLogger(__name__)

# Tracks active pairing WebSocket connections per terminal ID: terminal_id -> ws
ACTIVE_PAIRING_SOCKETS = {}


class CloverTerminal(models.Model):
    _name = "clover.terminal"
    _description = "Clover Payment Terminal"
    _order = "name asc"

    name = fields.Char(
        string="Terminal Name",
        required=True,
        help="Human-readable identifier (e.g. Counter 1 Flex, Mobile Flex 2).",
    )
    device_serial = fields.Char(
        string="Device Serial Number",
        required=True,
        index=True,
        help="Physical serial number of the Clover device (e.g. C143EQ202030587).",
    )
    model = fields.Selection(
        selection=[
            ("flex", "Clover Flex (Mobile/Handheld)"),
            ("mini_2", "Clover Mini 2nd Gen"),
            ("mini_3", "Clover Mini 3rd Gen"),
            ("station", "Clover Station / Duo"),
            ("other", "Other Clover Terminal"),
        ],
        string="Hardware Model",
        default="flex",
        required=True,
    )
    connection_mode = fields.Selection(
        selection=[
            ("snpd", "Secure Network Pay (SNPD Local WebSocket)"),
            ("lan", "Local Network (LAN REST Pay)"),
            ("cloud", "Clover Cloud Pay Display"),
        ],
        string="Connection Mode",
        default="snpd",
        required=True,
        help="SNPD: Direct local Wi-Fi — no developer account needed. "
             "LAN: Local REST Pay. Cloud: Clover Cloud Gateway.",
    )
    ip_address = fields.Char(
        string="Device IP Address",
        help="Local network IP address of the terminal (e.g. 192.168.10.95). Shown in SNPD app.",
    )
    port = fields.Integer(
        string="Port",
        default=12345,
        help="Server port: 12345 for SNPD, 12346 for REST Pay.",
    )
    use_https = fields.Boolean(
        string="Use WSS/HTTPS (SSL)",
        default=False,
        help="Connect over secure TLS/WSS. Required if the Clover app shows wss:// in its URL.",
    )
    pairing_auth_token = fields.Char(
        string="Auth Token",
        copy=False,
        help="The permanent authentication token Clover issued during pairing. Auto-saved after pairing completes.",
    )
    pairing_code = fields.Char(
        string="Pairing Code",
        readonly=True,
        help="The 4-digit code Clover generated. Enter it on the Clover touchscreen.",
    )
    is_paired = fields.Boolean(
        string="Is Paired",
        compute="_compute_is_paired",
        store=False,
    )

    @api.depends("pairing_auth_token")
    def _compute_is_paired(self):
        for record in self:
            token = (record.pairing_auth_token or "").strip()
            record.is_paired = bool(token and len(token) >= 32)

    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )
    last_status = fields.Char(string="Last Status", readonly=True)
    last_heartbeat = fields.Datetime(string="Last Verified At", readonly=True)
    notes = fields.Text(string="Internal Notes")

    _sql_constraints = [
        (
            "device_serial_company_uniq",
            "unique(device_serial, company_id)",
            "A Clover terminal with this device serial already exists for this company!",
        )
    ]

    @api.onchange("pairing_auth_token")
    def _onchange_pairing_auth_token(self):
        """Auto-parse pasted QR/WSS URL — extract token and connection parameters."""
        if not self.pairing_auth_token:
            return
        raw = self.pairing_auth_token.strip()
        if "authenticationToken=" in raw or raw.startswith("wss://") or raw.startswith("ws://"):
            try:
                parsed = urllib.parse.urlparse(raw)
                if parsed.scheme == "wss":
                    self.use_https = True
                elif parsed.scheme == "ws":
                    self.use_https = False
                if parsed.hostname:
                    self.ip_address = parsed.hostname
                if parsed.port:
                    self.port = parsed.port
                qs = urllib.parse.parse_qs(parsed.query)
                token = (
                    qs.get("authenticationToken")
                    or qs.get("authToken")
                    or qs.get("token")
                )
                if token:
                    self.pairing_auth_token = token[0].strip()
                    self.last_status = "Online / Paired (SNPD)"
                    return
            except Exception:
                pass
        self.pairing_auth_token = raw

    def _make_snpd_client(self, timeout: float = 10.0) -> "CloverSnpdClient":
        """Create a CloverSnpdClient from this terminal record."""
        return CloverSnpdClient(
            ip_address=self.ip_address,
            port=self.port or 12345,
            use_ssl=self.use_https,
            auth_token=(self.pairing_auth_token or "").strip(),
            device_serial=self.device_serial,
            pos_id=f"Odoo-POS-{self.name}",
            timeout=timeout,
        )

    # -------------------------------------------------------------------------
    # PAIRING FLOW
    # -------------------------------------------------------------------------
    def action_start_pairing(self):
        """
        Initiates pairing with Clover terminal:
        1. Connects to Clover over WebSocket and requests a fresh pairing code.
        2. KEEPS the WebSocket connection open so Clover can receive the code typed on the touchscreen.
        3. Spawns a background listener thread to capture the PAIRING_RESPONSE authToken.
        4. Opens the pairing wizard modal displaying the 4-digit code to the user.
        """
        self.ensure_one()
        if self.connection_mode != "snpd":
            raise UserError(_("Pairing is only supported in SNPD mode."))
        if not self.ip_address:
            raise UserError(_("Please set the Device IP Address first."))

        # Clean up any stale active socket for this terminal
        old_ws = ACTIVE_PAIRING_SOCKETS.pop(self.id, None)
        if old_ws:
            try:
                old_ws.close()
            except Exception:
                pass

        client = self._make_snpd_client(timeout=10.0)
        ws, code_or_token, err = client.start_pairing()

        if err:
            raise UserError(_(
                "Could not connect to Clover terminal:\n\n%s\n\n"
                "Make sure 'Secure Network Pay Display' is running on the Clover device.",
                err,
            ))

        # If Clover auto-paired immediately (e.g. existing valid session)
        if not ws and code_or_token and len(code_or_token) >= 32:
            self.write({
                "pairing_auth_token": code_or_token,
                "pairing_code": False,
                "last_status": "Online / Paired (SNPD)",
                "last_heartbeat": fields.Datetime.now(),
            })
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Already Paired!"),
                    "message": _("Terminal '%s' confirmed pairing.", self.name),
                    "sticky": False,
                    "type": "success",
                },
            }

        code = code_or_token
        ACTIVE_PAIRING_SOCKETS[self.id] = ws
        self.write({
            "pairing_code": code,
            "last_status": f"Pairing in progress — enter {code} on Clover",
        })

        # Launch background thread to listen on the open socket for Clover's PAIRING_RESPONSE
        db_name = self.env.cr.dbname
        terminal_id = self.id

        def _bg_listener():
            _logger.info("SNPD pairing listener started for terminal ID %s", terminal_id)
            success, token, msg = client.wait_for_pairing_token(ws, timeout=60.0)
            ACTIVE_PAIRING_SOCKETS.pop(terminal_id, None)
            try:
                from odoo.modules.registry import Registry
                registry = Registry(db_name)
                with registry.cursor() as cr:
                    env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                    term = env["clover.terminal"].browse(terminal_id)
                    if term.exists():
                        if success and token:
                            _logger.info("SNPD pairing SUCCESS for terminal ID %s, token: %s", terminal_id, token)
                            term.write({
                                "pairing_auth_token": token,
                                "pairing_code": False,
                                "last_status": "Online / Paired (SNPD)",
                                "last_heartbeat": fields.Datetime.now(),
                            })
                        else:
                            _logger.warning("SNPD pairing outcome for terminal ID %s: %s", terminal_id, msg)
                            term.write({
                                "last_status": f"Pairing Failed: {msg}",
                            })
                        cr.commit()
            except Exception as e:
                _logger.error("Error in SNPD pairing background listener: %s", e)

        threading.Thread(target=_bg_listener, daemon=True).start()

        # Open the Pairing Wizard modal
        wizard = self.env["clover.pairing.wizard"].create({
            "terminal_id": self.id,
            "pairing_code": code,
        })
        return {
            "name": _("Pair Clover Terminal"),
            "type": "ir.actions.act_window",
            "res_model": "clover.pairing.wizard",
            "view_mode": "form",
            "res_id": wizard.id,
            "target": "new",
        }

    # -------------------------------------------------------------------------
    # VERIFY SAVED TOKEN
    # -------------------------------------------------------------------------
    def action_verify_and_pair(self):
        """
        Verifies the saved authenticationToken against the live Clover device.
        """
        self.ensure_one()
        if self.connection_mode != "snpd":
            raise UserError(_("Token verification is only applicable for SNPD mode."))
        if not self.ip_address:
            raise UserError(_("Please set the Device IP Address first."))
        if not self.pairing_auth_token:
            raise UserError(_("No saved token. Please click 'Start Pairing' to pair the terminal."))

        client = self._make_snpd_client(timeout=8.0)
        res = client.verify_existing_token()

        if res.get("success"):
            self.write({
                "pairing_auth_token": res.get("auth_token", self.pairing_auth_token),
                "pairing_code": False,
                "last_status": "Online / Paired (SNPD)",
                "last_heartbeat": fields.Datetime.now(),
            })
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Token Verified — Terminal Ready"),
                    "message": _("Clover device '%s' accepted the saved token.", self.name),
                    "sticky": False,
                    "type": "success",
                },
            }
        else:
            self.write({
                "pairing_auth_token": False,
                "last_status": "Token Rejected — re-pair needed",
            })
            raise UserError(_(
                "Token verification failed: %s\n\n"
                "Click 'Start Pairing' to pair the terminal again.",
                res.get("message", "Token rejected."),
            ))

    # -------------------------------------------------------------------------
    # UNPAIR / CLEAR TOKEN
    # -------------------------------------------------------------------------
    def action_unpair_terminal(self):
        """Clears the saved token and closes any active pairing sockets."""
        self.ensure_one()
        ws = ACTIVE_PAIRING_SOCKETS.pop(self.id, None)
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        self.write({
            "pairing_auth_token": False,
            "pairing_code": False,
            "last_status": "Unpaired",
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Terminal Unpaired"),
                "message": _("Pairing credentials cleared for '%s'.", self.name),
                "sticky": False,
                "type": "info",
                "next": {"type": "ir.actions.client", "tag": "reload"},
            },
        }

    # -------------------------------------------------------------------------
    # TEST NETWORK PING
    # -------------------------------------------------------------------------
    def action_test_terminal_connection(self):
        """Tests TCP reachability of the terminal on the local network."""
        self.ensure_one()
        if self.connection_mode == "snpd":
            if not self.ip_address:
                raise UserError(_("Please enter the Device IP Address for SNPD mode."))
            port = self.port or 12345
            snpd = CloverSnpdClient(
                ip_address=self.ip_address,
                port=port,
                use_ssl=self.use_https,
                auth_token=self.pairing_auth_token,
                device_serial=self.device_serial,
            )
            res = snpd.test_tcp_connection()
            if res.get("success"):
                status_label = "Online / Paired (SNPD)" if self.is_paired else "Online / Ready to Pair (SNPD)"
                self.write({
                    "last_status": status_label,
                    "last_heartbeat": fields.Datetime.now(),
                })
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": _("Clover SNPD Terminal Reachable"),
                        "message": _(
                            "Device '%s' (%s) is online at %s:%s. %s",
                            self.name,
                            self.device_serial,
                            self.ip_address,
                            port,
                            "Paired and ready!" if self.is_paired else "Ready to pair (click 'Start Pairing').",
                        ),
                        "sticky": False,
                        "type": "success",
                    },
                }
            else:
                self.write({"last_status": "Offline / Unreachable"})
                raise UserError(res.get("message", _("Failed to reach terminal.")))
        else:
            payment_method = self.env["pos.payment.method"].search(
                [
                    ("clover_terminal_id", "=", self.id),
                    "|",
                    ("use_payment_terminal", "=", "clover_cloud"),
                    ("payment_provider", "=", "clover_cloud"),
                ],
                limit=1,
            )
            if self.connection_mode == "lan":
                if not self.ip_address:
                    raise UserError(_("Please enter the Device IP Address for Local LAN mode."))
                scheme = "https" if self.use_https else "http"
                port = self.port or 12346
                base_url = f"{scheme}://{self.ip_address.strip()}:{port}"
                token = payment_method.clover_api_token if payment_method else ""
                client = CloverApiClient(
                    api_token=token,
                    custom_base_url=base_url,
                    verify_ssl=False,
                    pos_id=f"Odoo-POS-{self.id}",
                )
            else:
                if not payment_method or not payment_method.clover_api_token:
                    raise UserError(_(
                        "For Cloud mode, please link this terminal to a POS Payment Method "
                        "with your Clover API Token first."
                    ))
                client = payment_method._get_clover_client()

            result = client.test_connection(self.device_serial)
            if result.get("success"):
                status_label = f"Online / Responsive ({'LAN' if self.connection_mode == 'lan' else 'Cloud'})"
                self.write({
                    "last_status": status_label,
                    "last_heartbeat": fields.Datetime.now(),
                })
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": _("Clover Connection Successful"),
                        "message": _("Device '%s' (%s) is responsive.", self.name, self.device_serial),
                        "sticky": False,
                        "type": "success",
                    },
                }
            else:
                self.write({"last_status": "Offline / Error"})
                raise UserError(result.get("message", _("Failed to communicate with device.")))

    # -------------------------------------------------------------------------
    # TEST £0.01 SALE
    # -------------------------------------------------------------------------
    def action_send_test_payment(self):
        """Dispatches an immediate £0.01 test sale to verify live card interaction."""
        self.ensure_one()
        if self.connection_mode != "snpd":
            raise UserError(_("Test payment is only supported in SNPD mode."))
        if not self.is_paired:
            raise UserError(_(
                "Terminal is not paired yet. Please click 'Start Pairing' to pair the device first."
            ))

        client = self._make_snpd_client(timeout=60.0)
        test_id = f"test_{int(fields.Datetime.now().timestamp())}"
        currency = self.company_id.currency_id.name or "GBP"
        try:
            res = client.send_sale_request(
                amount_cents=1,
                external_payment_id=test_id,
                currency=currency,
                prompt_tip=False,
                print_receipt=False,
            )
            if client.auth_token and client.auth_token != self.pairing_auth_token:
                self.write({"pairing_auth_token": client.auth_token})
            card_tx = res.get("cardTransaction", {})
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Test Payment Approved!"),
                    "message": _(
                        "£0.01 authorized on %s! Card: %s ****%s (Auth: %s)",
                        self.name,
                        card_tx.get("cardType", "Card"),
                        card_tx.get("last4", "????"),
                        card_tx.get("authCode", "OK"),
                    ),
                    "sticky": True,
                    "type": "success",
                },
            }
        except CloverSnpdException as exc:
            raise UserError(_("Test payment failed: %s", str(exc)))


class CloverPairingWizard(models.TransientModel):
    _name = "clover.pairing.wizard"
    _description = "Clover Terminal Pairing Wizard"

    terminal_id = fields.Many2one("clover.terminal", string="Terminal", required=True)
    device_name = fields.Char(related="terminal_id.name", readonly=True)
    pairing_code = fields.Char(string="Pairing Code", required=True, readonly=True)
    manual_auth_token = fields.Char(
        string="Or Paste Token / QR URL",
        help="Optional: If you scanned the QR code on the Clover screen, paste it here.",
    )

    def action_verify_pairing(self):
        """
        Called when the merchant clicks 'Complete Pairing' after typing the code on the Clover screen.
        Checks if the background listener has captured the authToken.
        """
        self.ensure_one()
        terminal = self.terminal_id

        # If user pasted manual token
        if self.manual_auth_token:
            token = self.manual_auth_token.strip()
            if "authenticationToken=" in token or "authToken=" in token:
                try:
                    parsed = urllib.parse.urlparse(token)
                    qs = urllib.parse.parse_qs(parsed.query)
                    token = (qs.get("authenticationToken") or qs.get("authToken") or [token])[0]
                except Exception:
                    pass
            terminal.write({
                "pairing_auth_token": token,
                "pairing_code": False,
                "last_status": "Online / Paired (SNPD)",
                "last_heartbeat": fields.Datetime.now(),
            })
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Pairing Complete!"),
                    "message": _("Terminal '%s' paired with manual token.", terminal.name),
                    "sticky": False,
                    "type": "success",
                    "next": {"type": "ir.actions.client", "tag": "reload"},
                },
            }

        # Wait up to 5 seconds in case background thread is processing the response
        start_wait = time.time()
        while time.time() - start_wait < 5.0:
            terminal.invalidate_recordset(["pairing_auth_token", "last_status"])
            if terminal.pairing_auth_token and len(terminal.pairing_auth_token) >= 32:
                break
            time.sleep(0.5)

        if terminal.pairing_auth_token and len(terminal.pairing_auth_token) >= 32:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Pairing Complete!"),
                    "message": _("Terminal '%s' is now paired and ready for payments.", terminal.name),
                    "sticky": False,
                    "type": "success",
                    "next": {"type": "ir.actions.client", "tag": "reload"},
                },
            }
        else:
            raise UserError(_(
                "Clover has not confirmed the code '%s' yet.\n\n"
                "Please make sure you:\n"
                "1. Entered your Manager Passcode on Clover (if prompted).\n"
                "2. Typed '%s' on the Clover touchscreen.\n\n"
                "Once Clover shows 'Client connected', click 'Complete Pairing' again.",
                self.pairing_code,
                self.pairing_code,
            ))
