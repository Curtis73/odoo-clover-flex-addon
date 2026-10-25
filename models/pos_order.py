# -*- coding: utf-8 -*-
import logging
from odoo import models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PosOrder(models.Model):
    _inherit = "pos.order"

    def _process_saved_order(self, draft):
        """
        Enforce atomic payment validation:
        An order containing a Clover Card payment line MUST have a verified, approved
        Clover transaction record in the database before the sale can be finalized.
        """
        if not draft:
            for order in self:
                for payment in order.payment_ids:
                    pm = payment.payment_method_id
                    is_clover = (
                        pm.use_payment_terminal == "clover_cloud"
                        or pm.payment_provider == "clover_cloud"
                    )
                    if is_clover:
                        tx_id = (payment.transaction_id or "").strip()
                        approved_tx = None
                        if tx_id:
                            approved_tx = self.env["clover.transaction"].sudo().search([
                                "|",
                                ("external_payment_id", "=", tx_id),
                                ("clover_payment_id", "=", tx_id),
                                ("state", "=", "success"),
                            ], limit=1)

                        if not approved_tx:
                            _logger.error(
                                "Atomic validation failed for order %s: Clover payment of %s has no approved transaction (tx_id: %s)",
                                order.pos_reference or order.name,
                                payment.amount,
                                tx_id,
                            )
                            raise UserError(_(
                                "Order '%s' cannot be validated: Card payment of %s %s was not authorized by the Clover terminal.",
                                order.pos_reference or order.name,
                                payment.amount,
                                order.currency_id.name or "",
                            ))
        return super()._process_saved_order(draft)
