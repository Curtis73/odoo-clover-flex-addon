# -*- coding: utf-8 -*-
import random
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
        help="Physical serial number of the Clover device (e.g. C143EQ20230587).",
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
        help="SNPD: Direct local Wi-Fi pairing code (no developer account needed, instant). "
             "LAN: Local REST Pay. Cloud: Clover Cloud Gateway.",
    )
    ip_address = fields.Char(
        string="Device IP Address",
        help="Local network IP address of the terminal (e.g. 192.168.10.95). Shown in the app.",
    )
    port = fields.Integer(
        string="Port",
        default=12345,
        help="Server port: 12345 for SNPD, 12346 for REST Pay.",
    )
    use_https = fields.Boolean(
        string="Use HTTPS / WSS (SSL)",
        default=False,
        help="Connect over secure TLS/WSS. Keep unchecked if unencrypted WS/HTTP is selected in device settings.",
    )
    pairing_code = fields.Char(
        string="Pairing Code",
        readonly=True,
        help="4-digit pairing code to enter on the Clover terminal screen.",
    )
    pairing_auth_token = fields.Char(
        string="Pairing Auth Token",
        copy=False,
        help="Permanent authorization token issued by the Clover device upon successful pairing.",
    )
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )
    last_status = fields.Char(string="Last Cloud Status", readonly=True)
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
        """Automatically sanitizes pasted QR code URLs or direct tokens."""
        if self.pairing_auth_token:
            raw = self.pairing_auth_token.strip()
            # If user pasted the whole QR URL: wss://192.168.10.95:12345/remote_pay?authenticationToken=...
            if "authenticationToken=" in raw or "authToken=" in raw or "token=" in raw:
                import urllib.parse
                try:
                    parsed = urllib.parse.urlparse(raw)
                    if parsed.hostname and not self.ip_address:
                        self.ip_address = parsed.hostname
                    if parsed.port and not self.port:
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
            if "?" in raw:
                for part in raw.split("?")[1].split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        if k.lower() in ("authenticationtoken", "authtoken", "token"):
                            self.pairing_auth_token = v.strip()
                            self.last_status = "Online / Paired (SNPD)"
                            return
            self.pairing_auth_token = raw
            if raw:
                self.last_status = "Online / Paired (SNPD)"

    def action_pair_terminal(self):
        """
        Initiates 4-digit code pairing with the Clover device running Secure Network Pay Display.
        """
        self.ensure_one()
        if not self.ip_address:
            raise UserError(_("Please enter the Device IP Address first."))

        # Generate fresh 4-digit pairing PIN
        code = f"{random.randint(1000, 9999)}"
        self.pairing_code = code

        port = self.port or 12345
        client = CloverSnpdClient(
            ip_address=self.ip_address,
            port=port,
            use_ssl=self.use_https,
            device_serial=self.device_serial,
            pos_id=f"Odoo-POS-{self.name}",
            timeout=15.0,
        )

        res = client.initiate_pairing(pairing_code=code)
        if res.get("success") and res.get("auth_token"):
            self.write({
                "pairing_auth_token": res["auth_token"],
                "last_status": "Online / Paired (SNPD)",
                "last_heartbeat": fields.Datetime.now(),
            })
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Clover Pairing Successful!"),
                    "message": _("Terminal '%s' is paired and authenticated.", self.name),
                    "sticky": False,
                    "type": "success",
                },
            }
        else:
            # If prompt was displayed on device
            self.write({"last_status": "Pairing Pending / Confirm Code"})
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Enter Pairing Code On Device"),
                    "message": _(
                        "Pairing request sent to %s:%s. Enter code '%s' on your Clover screen to authorize.",
                        self.ip_address,
                        port,
                        code,
                    ),
                    "sticky": True,
                    "type": "warning",
                },
            }

    def action_unpair_terminal(self):
        """Clears pairing credentials for this terminal."""
        self.ensure_one()
        self.write({
            "pairing_code": False,
            "pairing_auth_token": False,
            "last_status": "Unpaired",
        })
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Terminal Unpaired"),
                "message": _("Pairing credentials removed for '%s'.", self.name),
                "sticky": False,
                "type": "info",
            },
        }

    def action_test_terminal_connection(self):
        """
        Tests connectivity between this terminal and Odoo (SNPD, Local LAN, or Clover Cloud).
        """
        self.ensure_one()
        payment_method = self.env["pos.payment.method"].search(
            [
                ("clover_terminal_id", "=", self.id),
                ("use_payment_terminal", "=", "clover_cloud"),
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
                status_label = "Online / Paired (SNPD)" if self.pairing_auth_token else "Online / Ready to Pair (SNPD)"
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
                            "Device '%s' (%s) is reachable on local network at %s:%s. %s",
                            self.name,
                            self.device_serial,
                            self.ip_address,
                            port,
                            "Paired & authenticated!" if self.pairing_auth_token else "Ready to pair (click 'Pair Terminal').",
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
                raise UserError(
                    _(
                        "For Cloud mode, please configure and link this terminal to a POS Payment Method "
                        "with your Clover API Token first."
                    )
                )
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

    def action_send_test_payment(self):
        """Dispatches an immediate £0.01 test sale to verify live device card interaction."""
        self.ensure_one()
        if self.connection_mode != "snpd":
            raise UserError(_("Test payment button is currently configured for SNPD mode."))
        if not self.pairing_auth_token:
            raise UserError(_("Terminal is not paired yet. Please paste the pairing token first."))

        port = self.port or 12345
        client = CloverSnpdClient(
            ip_address=self.ip_address,
            port=port,
            use_ssl=self.use_https,
            auth_token=self.pairing_auth_token,
            device_serial=self.device_serial,
            pos_id=f"Odoo-POS-{self.name}",
            timeout=30.0,
        )

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
                        "Payment of £0.01 authorized on %s! Card: %s ****%s (Auth: %s)",
                        self.name,
                        card_tx.get("cardType", "Card"),
                        card_tx.get("last4", "0000"),
                        card_tx.get("authCode", "OK"),
                    ),
                    "sticky": True,
                    "type": "success",
                },
            }
        except Exception as exc:
            raise UserError(_("Test payment failed: %s", str(exc)))
