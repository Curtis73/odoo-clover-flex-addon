# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError
try:
    from ..services.clover_api_client import CloverApiClient, CloverApiException
    from ..services.clover_snpd_client import CloverSnpdClient, CloverSnpdException
except (ImportError, ValueError):
    from services.clover_api_client import CloverApiClient, CloverApiException
    from services.clover_snpd_client import CloverSnpdClient, CloverSnpdException


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
        help="The 4-digit code Clover generated. Enter it on the Clover touchscreen, then click 'Confirm Pairing'.",
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
            # A real Clover token is a 36-char UUID
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

    # -------------------------------------------------------------------------
    # Helper
    # -------------------------------------------------------------------------
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
    # STEP 1 — Start Pairing
    # Connects to Clover without a token; Clover responds with a 4-digit code.
    # That code is shown to the user so they can type it on the Clover screen.
    # -------------------------------------------------------------------------
    def action_start_pairing(self):
        """
        Phase 1 of SNPD pairing.

        Clover's actual flow:
          1. Client connects with empty / unknown authenticationToken
          2. Clover responds: PAIRING_CODE { pairingCode: "XXXX" }
          3. User types XXXX on the Clover touchscreen
          4. Clover responds: PAIRING_RESPONSE { pairingState: "PAIRED",
                                                 authenticationToken: "<uuid>" }
          5. Client saves that UUID — it is permanent until the SNPD server restarts.

        This method handles step 1-2 and saves the code for step 3-4 (Confirm Pairing).
        """
        self.ensure_one()
        if self.connection_mode != "snpd":
            raise UserError(_("Pairing is only supported in SNPD mode."))
        if not self.ip_address:
            raise UserError(_("Please set the Device IP Address first."))

        client = self._make_snpd_client(timeout=8.0)
        res = client.get_pairing_code()

        if not res.get("success"):
            raise UserError(_(
                "Could not connect to Clover terminal:\n\n%s\n\n"
                "Make sure the 'Secure Network Pay Display' app is open and running on the Clover device.",
                res.get("message", "Unknown error"),
            ))

        if res.get("already_paired") and res.get("auth_token"):
            # Edge case: Clover auto-confirmed without issuing a code
            token = res["auth_token"]
            self.write({
                "pairing_auth_token": token,
                "pairing_code": False,
                "last_status": "Online / Paired (SNPD)",
                "last_heartbeat": fields.Datetime.now(),
            })
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Already Paired!"),
                    "message": _("Terminal '%s' confirmed pairing automatically and is ready.", self.name),
                    "sticky": False,
                    "type": "success",
                },
            }

        code = res.get("pairing_code", "????")
        self.write({
            "pairing_code": code,
            "pairing_auth_token": False,
            "last_status": f"Pairing in progress — enter {code} on Clover",
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Enter code %s on your Clover screen now", code),
                "message": _(
                    "The Clover Flex is showing a keypad. "
                    "Type  %s  on the touchscreen.\n\n"
                    "Then click 'Confirm Pairing' on this page.",
                    code,
                ),
                "sticky": True,
                "type": "warning",
            },
        }

    # -------------------------------------------------------------------------
    # STEP 2 — Confirm Pairing
    # Reconnects and waits for Clover to confirm the user entered the code.
    # Captures the permanent authenticationToken.
    # -------------------------------------------------------------------------
    def action_confirm_pairing(self):
        """
        Phase 2 of SNPD pairing.

        Reconnects to Clover and waits up to 45 seconds for the user to type
        the 4-digit code on the Clover screen.  Clover then sends
        PAIRING_RESPONSE { pairingState: "PAIRED", authenticationToken: "<uuid>" }.
        That UUID is saved permanently.
        """
        self.ensure_one()
        if not self.pairing_code:
            raise UserError(_(
                "No pairing code found. Please click 'Start Pairing' first."
            ))

        client = self._make_snpd_client(timeout=50.0)
        res = client.confirm_pairing(pairing_code=self.pairing_code, wait_seconds=45.0)

        if res.get("success") and res.get("auth_token"):
            token = res["auth_token"]
            self.write({
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
                    "message": _(
                        "Terminal '%s' (%s) is now permanently paired and ready for payments.",
                        self.name, self.device_serial,
                    ),
                    "sticky": False,
                    "type": "success",
                },
            }
        else:
            self.write({"last_status": "Pairing Failed"})
            raise UserError(_(
                "Pairing failed: %s\n\n"
                "Click 'Start Pairing' again to get a fresh code and retry.",
                res.get("message", "Unknown error"),
            ))

    # -------------------------------------------------------------------------
    # Verify an existing saved token
    # -------------------------------------------------------------------------
    def action_verify_and_pair(self):
        """
        Verifies the saved authenticationToken against the live Clover device.
        Use this after a Clover restart to check the token is still valid without
        going through the full pairing flow again.
        """
        self.ensure_one()
        if self.connection_mode != "snpd":
            raise UserError(_("Token verification is only applicable for SNPD mode."))
        if not self.ip_address:
            raise UserError(_("Please set the Device IP Address first."))
        if not self.pairing_auth_token:
            raise UserError(_(
                "No saved token. Please click 'Start Pairing' to pair the terminal."
            ))

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
                    "message": _(
                        "Clover device '%s' accepted the saved token and is ready for payments.",
                        self.name,
                    ),
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
    # Clear token / unpair
    # -------------------------------------------------------------------------
    def action_unpair_terminal(self):
        """Clears the saved token and pairing code."""
        self.ensure_one()
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
            },
        }

    # -------------------------------------------------------------------------
    # Ping / TCP check
    # -------------------------------------------------------------------------
    def action_test_terminal_connection(self):
        """Tests TCP reachability of the terminal on the local network."""
        self.ensure_one()
        payment_method = self.env["pos.payment.method"].search(
            [
                ("clover_terminal_id", "=", self.id),
                "|",
                ("use_payment_terminal", "=", "clover_cloud"),
                ("payment_provider", "=", "clover_cloud"),
            ],
            limit=1,
        )

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
                status_label = "Online / Paired (SNPD)" if self.is_paired else "Online / Not Paired (SNPD)"
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
                            "Paired and ready!" if self.is_paired else "Not paired yet — click 'Start Pairing'.",
                        ),
                        "sticky": False,
                        "type": "success",
                    },
                }
            else:
                self.write({"last_status": "Offline / Unreachable"})
                raise UserError(res.get("message", _("Failed to reach terminal.")))

        elif self.connection_mode == "lan":
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
                    "message": _(
                        "Device '%s' (%s) is verified and responsive via %s.",
                        self.name,
                        self.device_serial,
                        "Local LAN" if self.connection_mode == "lan" else "Clover Cloud",
                    ),
                    "sticky": False,
                    "type": "success",
                },
            }
        else:
            self.write({"last_status": "Offline / Error"})
            raise UserError(result.get("message", _("Failed to communicate with device.")))

    # -------------------------------------------------------------------------
    # Test £0.01 Payment
    # -------------------------------------------------------------------------
    def action_send_test_payment(self):
        """Dispatches an immediate £0.01 test sale to verify live card interaction."""
        self.ensure_one()
        if self.connection_mode != "snpd":
            raise UserError(_("Test payment is only supported in SNPD mode."))
        if not self.is_paired:
            raise UserError(_(
                "Terminal is not paired yet. Click 'Start Pairing' and follow the steps."
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
            card_tx = res.get("cardTransaction", {})
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Test Payment Approved!"),
                    "message": _(
                        "£0.01 authorised on %s — %s ****%s (Auth: %s)",
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
