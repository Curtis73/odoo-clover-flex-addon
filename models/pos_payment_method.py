# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
try:
    from ..services.clover_api_client import CloverApiClient, CloverApiException
except (ImportError, ValueError):
    from services.clover_api_client import CloverApiClient, CloverApiException


_logger = logging.getLogger(__name__)


class PosPaymentMethod(models.Model):
    _inherit = "pos.payment.method"

    def _get_payment_terminal_selection(self):
        try:
            res = super()._get_payment_terminal_selection()
        except AttributeError:
            res = [("none", _("None"))]
        if not any(code == "clover_cloud" for code, _label in res):
            res.append(("clover_cloud", _("Clover Cloud Pay (Flex / Mini)")))
        return res

    use_payment_terminal = fields.Selection(
        selection=_get_payment_terminal_selection,
        string="Payment Terminal",
        default="none",
        help="Select whether this payment method connects to a card terminal.",
    )

    clover_terminal_id = fields.Many2one(
        "clover.terminal",
        string="Clover Terminal",
        domain="[('company_id', '=', company_id), ('active', '=', True)]",
        help="Physical Clover Flex or Mini device assigned to this payment method.",
    )
    clover_environment = fields.Selection(
        selection=[
            ("sandbox", "Sandbox (dev.clover.com)"),
            ("prod_eu", "Production UK / Europe (api.eu.clover.com)"),
            ("prod_us", "Production US / Canada (api.clover.com)"),
        ],
        string="Clover Environment",
        default="prod_eu",
        help="Select the region matching your Clover merchant account.",
    )
    clover_merchant_id = fields.Char(
        string="Merchant ID (Optional)",
        copy=False,
        help="Your Clover 13-character Merchant ID (optional with Cloud Pay Display).",
    )
    clover_api_token = fields.Char(
        string="Clover API Token",
        copy=False,
        help="Bearer API token with Ecommerce / Cloud Pay permissions generated in Developer Settings.",
    )
    clover_allow_tip = fields.Boolean(
        string="Customer Tip on Device",
        default=False,
        help="If enabled, the Clover screen will prompt the customer for a tip before card entry.",
    )
    clover_auto_print_receipt = fields.Boolean(
        string="Print on Clover Terminal",
        default=True,
        help="If enabled, prints the transaction receipt directly on the Clover Flex internal printer.",
    )

    @api.constrains("use_payment_terminal", "clover_terminal_id", "clover_api_token")
    def _check_clover_configuration(self):
        for pm in self:
            if pm.use_payment_terminal == "clover_cloud":
                if not pm.clover_terminal_id:
                    raise ValidationError(_("You must select a Clover Terminal for Clover Cloud Pay."))
                if not pm.clover_api_token:
                    raise ValidationError(_("Clover API Token is required."))

    def _get_clover_client(self) -> CloverApiClient:
        """Instantiates a configured CloverApiClient for this payment method."""
        self.ensure_one()
        return CloverApiClient(
            api_token=self.clover_api_token,
            merchant_id=self.clover_merchant_id,
            environment=self.clover_environment or "prod_eu",
            pos_id=f"Odoo20-POS-{self.id}",
        )

    def _amount_to_cents(self, amount: float) -> int:
        """Converts currency amount to minor units (e.g. pence/cents)."""
        currency = self.company_id.currency_id or self.env.company.currency_id
        dec = getattr(currency, "decimal_places", 2)
        decimal_places = 2 if dec is None else dec
        factor = 10 ** decimal_places
        return int(round(amount * factor))

    @api.model
    def action_clover_initiate_payment(self, payment_method_id: int, amount: float, external_id: str):
        """
        RPC Entrypoint called by Odoo POS OWL frontend when cashier clicks Clover payment.
        """
        pm = self.browse(payment_method_id)
        if not pm.exists() or pm.use_payment_terminal != "clover_cloud":
            return {"success": False, "error": _("Invalid payment method configuration.")}

        if amount <= 0:
            return {"success": False, "error": _("Payment amount must be greater than zero.")}

        terminal = pm.clover_terminal_id
        if not terminal or not terminal.device_serial:
            return {"success": False, "error": _("No Clover terminal device serial configured.")}

        # 1. Create or retrieve transaction audit record (Idempotency enforcement)
        transaction_model = self.env["clover.transaction"].sudo()
        currency = pm.company_id.currency_id or self.env.company.currency_id
        tx = transaction_model.search([("external_payment_id", "=", external_id)], limit=1)
        if not tx:
            tx = transaction_model.create({
                "external_payment_id": external_id,
                "pos_payment_method_id": pm.id,
                "clover_terminal_id": terminal.id,
                "amount": amount,
                "currency_id": currency.id,
                "company_id": pm.company_id.id,
                "state": "pending",
            })

        # 2. Dispatch request to Clover Cloud
        client = pm._get_clover_client()
        amount_cents = pm._amount_to_cents(amount)

        try:
            response = client.send_payment_request(
                device_serial=terminal.device_serial,
                amount_cents=amount_cents,
                external_payment_id=external_id,
                currency=currency.name or "GBP",
                prompt_tip=pm.clover_allow_tip,
                auto_accept=True,
                print_receipt=pm.clover_auto_print_receipt,
            )

            result_code = response.get("result") or response.get("status") or ""
            if result_code.upper() in ("SUCCESS", "APPROVED", "PAID"):
                card_tx = response.get("cardTransaction", {})
                clover_pid = response.get("id", "")
                auth_code = card_tx.get("authCode") or response.get("authCode", "")
                card_brand = card_tx.get("cardType") or response.get("cardType", "Card")
                card_last4 = card_tx.get("last4") or ""

                tx.write({
                    "state": "success",
                    "clover_payment_id": clover_pid,
                    "auth_code": auth_code,
                    "card_brand": card_brand,
                    "card_last4": card_last4[:4] if card_last4 else "",
                })
                return {
                    "success": True,
                    "state": "success",
                    "clover_payment_id": clover_pid,
                    "auth_code": auth_code,
                    "card_brand": card_brand,
                    "card_last4": card_last4,
                }

            return {
                "success": True,
                "state": "pending",
                "message": _("Payment dispatched to Clover terminal."),
            }
        except CloverApiException as exc:
            tx.write({
                "state": "failed",
                "error_message": str(exc),
            })
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            _logger.exception("Unexpected error initiating Clover payment:")
            tx.write({
                "state": "failed",
                "error_message": str(exc),
            })
            return {"success": False, "error": _("Internal error connecting to Clover terminal.")}

    @api.model
    def action_clover_check_status(self, payment_method_id: int, external_id: str):
        """
        RPC Entrypoint polled by Odoo POS OWL frontend to check payment completion.
        """
        pm = self.browse(payment_method_id)
        if not pm.exists():
            return {"success": False, "error": _("Payment method not found.")}

        tx = self.env["clover.transaction"].sudo().search(
            [("external_payment_id", "=", external_id)], limit=1
        )
        if not tx:
            return {"success": False, "error": _("Transaction not found for ID: %s") % external_id}

        if tx.state in ("success", "failed", "cancelled"):
            return {
                "success": tx.state == "success",
                "state": tx.state,
                "clover_payment_id": tx.clover_payment_id,
                "auth_code": tx.auth_code,
                "card_brand": tx.card_brand,
                "card_last4": tx.card_last4,
                "error": tx.error_message,
            }

        client = pm._get_clover_client()
        terminal = pm.clover_terminal_id

        try:
            data = client.get_payment_status(
                terminal.device_serial,
                external_payment_id=external_id,
                clover_payment_id=tx.clover_payment_id or None,
            )

            if "elements" in data and isinstance(data["elements"], list) and data["elements"]:
                data = data["elements"][0]

            status_val = data.get("result") or data.get("status") or ""
            payment_obj = data.get("payment") if isinstance(data.get("payment"), dict) else data

            if status_val.upper() in ("SUCCESS", "PAID", "APPROVED") or (payment_obj and payment_obj.get("id") and status_val.upper() not in ("FAIL", "FAILED", "DECLINED")):
                card_tx = payment_obj.get("cardTransaction", {})
                clover_pid = payment_obj.get("id") or data.get("id", "")
                auth_code = card_tx.get("authCode") or payment_obj.get("authCode", "")
                card_brand = card_tx.get("cardType") or payment_obj.get("cardType", "Card")
                card_last4 = card_tx.get("last4") or ""

                tx.write({
                    "state": "success",
                    "clover_payment_id": clover_pid,
                    "auth_code": auth_code,
                    "card_brand": card_brand,
                    "card_last4": card_last4[:4] if card_last4 else "",
                })

                return {
                    "success": True,
                    "state": "success",
                    "clover_payment_id": clover_pid,
                    "auth_code": auth_code,
                    "card_brand": card_brand,
                    "card_last4": card_last4,
                }
            elif status_val.upper() in ("FAIL", "FAILED", "DECLINED"):
                reason = data.get("reason") or data.get("message") or "Payment declined on device."
                tx.write({"state": "failed", "error_message": reason})
                return {"success": False, "state": "failed", "error": reason}
            elif status_val.upper() in ("CANCEL", "CANCELLED", "VOIDED"):
                tx.write({"state": "cancelled", "error_message": "Cancelled by customer/terminal."})
                return {"success": False, "state": "cancelled", "error": "Transaction cancelled."}
            else:
                return {"success": True, "state": "pending"}

        except CloverApiException as exc:
            _logger.warning("Error querying Clover payment status: %s", str(exc))
            return {"success": True, "state": "pending", "warning": str(exc)}

    @api.model
    def action_clover_cancel_payment(self, payment_method_id: int, external_id: str):
        """
        RPC Entrypoint invoked when cashier cancels payment in POS.
        Sends cancel signal to Clover terminal to clear customer display.
        """
        pm = self.browse(payment_method_id)
        if not pm.exists():
            return {"success": False, "error": _("Payment method not found.")}

        terminal = pm.clover_terminal_id
        tx = self.env["clover.transaction"].sudo().search(
            [("external_payment_id", "=", external_id)], limit=1
        )

        try:
            client = pm._get_clover_client()
            client.cancel_active_payment(terminal.device_serial, external_id)
            if tx:
                tx.write({"state": "cancelled", "error_message": "Cancelled from POS interface."})
            return {"success": True}
        except Exception as exc:
            _logger.warning("Failed to send cancel to Clover device: %s", str(exc))
            if tx:
                tx.write({"state": "cancelled", "error_message": str(exc)})
            return {"success": True}
