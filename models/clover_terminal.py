# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError
try:
    from ..services.clover_api_client import CloverApiClient, CloverApiException
except (ImportError, ValueError):
    from services.clover_api_client import CloverApiClient, CloverApiException


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
            ("lan", "Local Network (LAN REST Pay)"),
            ("cloud", "Clover Cloud Pay Display"),
        ],
        string="Connection Mode",
        default="lan",
        required=True,
        help="LAN mode communicates directly with the terminal on your local Wi-Fi (fast, no cloud token required). Cloud mode connects through Clover servers.",
    )
    ip_address = fields.Char(
        string="Device IP Address",
        help="Local network IP address of the terminal (e.g. 192.168.1.150). Shown in the REST Pay Display app.",
    )
    port = fields.Integer(
        string="Port",
        default=12346,
        help="Local REST Pay server port (default is 12346).",
    )
    use_https = fields.Boolean(
        string="Use HTTPS (SSL)",
        default=True,
        help="Connect over secure TLS. Uncheck only if Server is Secured is toggled off in device settings.",
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

    def action_test_terminal_connection(self):
        """
        Tests connectivity between this terminal and Odoo (Local LAN or Clover Cloud).
        """
        self.ensure_one()
        payment_method = self.env["pos.payment.method"].search(
            [
                ("clover_terminal_id", "=", self.id),
                ("use_payment_terminal", "=", "clover_cloud"),
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
