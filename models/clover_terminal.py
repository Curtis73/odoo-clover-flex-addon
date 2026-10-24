# -*- coding: utf-8 -*-
"""
Clover Terminal Model.
Represents individual physical Clover hardware terminals (Flex, Mini, etc.)
registered to a merchant.
"""

try:
    from odoo import api, fields, models, _
    from odoo.exceptions import UserError, ValidationError
except ImportError:
    # Standalone fallback for testing/CI when full Odoo ERP is not in PYTHONPATH
    class _Dummy:
        def __init__(self, *args, **kwargs): pass
        def __call__(self, *args, **kwargs): return lambda f: f
        def __getattr__(self, name): return _Dummy()
    api = fields = models = _Dummy()
    models.Model = object
    _ = lambda s, *args: s
    class UserError(Exception): pass
    class ValidationError(Exception): pass


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
        help="Physical serial number of the Clover device (e.g. C031UQ52340015). "
             "Can be found on the device back label or in Settings > About Device.",
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
        Tests connectivity between this terminal and Clover Cloud.
        Finds a payment method linked to this terminal to obtain merchant credentials.
        """
        self.ensure_one()
        payment_method = self.env["pos.payment.method"].search(
            [
                ("clover_terminal_id", "=", self.id),
                ("use_payment_terminal", "=", "clover_cloud"),
            ],
            limit=1,
        )

        if not payment_method:
            raise UserError(
                _(
                    "Please configure and link this terminal to a POS Payment Method "
                    "with your Clover API Token first."
                )
            )

        client = payment_method._get_clover_client()
        try:
            result = client.test_connection(self.device_serial)
            if result.get("success"):
                self.write({
                    "last_status": "Online / Registered",
                    "last_heartbeat": fields.Datetime.now(),
                })
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": _("Clover Connection Successful"),
                        "message": _(
                            "Device '%s' (%s) is verified on Clover Cloud.",
                            self.name,
                            self.device_serial,
                        ),
                        "sticky": False,
                        "type": "success",
                    },
                }
            else:
                self.write({"last_status": "Device Not Found in Merchant Account"})
                raise UserError(result.get("message"))
        except Exception as exc:
            self.write({"last_status": f"Error: {str(exc)[:100]}"})
            raise UserError(_("Clover Cloud Connection Failed: %s") % str(exc))
