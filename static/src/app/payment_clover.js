/** @odoo-module */

import { PaymentInterface } from "@point_of_sale/app/payment/payment_interface";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";

/**
 * Clover Cloud Pay Display Payment Interface for Odoo 20 POS.
 * Connects POS OWL checkout with Clover Flex / Clover Mini terminals via secure backend gateway.
 */
export class PaymentCloverCloud extends PaymentInterface {
    setup() {
        super.setup(...arguments);
        this.pollTimeouts = new Map();
    }

    /**
     * Dispatches payment request when the cashier selects this payment method.
     * @param {string} cid - Payment line CID
     * @returns {Promise<boolean>}
     */
    async send_payment_request(cid) {
        return this.sendPaymentRequest(cid);
    }

    async sendPaymentRequest(cid) {
        if (super.sendPaymentRequest) {
            await super.sendPaymentRequest(cid);
        } else if (super.send_payment_request) {
            await super.send_payment_request(cid);
        }


        const order = this.pos.get_order();
        const line = order ? order.get_paymentline(cid) : null;
        if (!line) {
            return false;
        }

        const externalId = crypto.randomUUID();
        line.terminal_service_id = externalId;
        line.set_payment_status("waitingCard");

        let initResponse;
        try {
            initResponse = await this.env.services.orm.silent.call(
                "pos.payment.method",
                "action_clover_initiate_payment",
                [this.payment_method.id, line.amount, externalId]
            );
        } catch (error) {
            line.set_payment_status("retry");
            this._showError(_t("Failed to communicate with Odoo server: ") + (error.message || error));
            return false;
        }

        if (!initResponse || !initResponse.success) {
            line.set_payment_status("retry");
            this._showError((initResponse && initResponse.error) || _t("Could not contact Clover terminal."));
            return false;
        }

        if (initResponse.state === "success") {
            line.transaction_id = initResponse.clover_payment_id || externalId;
            line.card_type = initResponse.card_brand || "Card";
            if (initResponse.card_last4) {
                line.card_last4 = initResponse.card_last4;
            }
            line.set_payment_status("done");
            return true;
        }

        return this._pollPaymentStatus(cid, externalId);
    }

    /**
     * Asynchronously polls Odoo backend for terminal transaction updates.
     */
    _pollPaymentStatus(cid, externalId) {
        return new Promise((resolve) => {
            const checkStatus = async () => {
                const order = this.pos.get_order();
                const line = order ? order.get_paymentline(cid) : null;

                if (!line || line.terminal_service_id !== externalId) {
                    this._clearPolling(cid);
                    resolve(false);
                    return;
                }

                try {
                    const res = await this.env.services.orm.silent.call(
                        "pos.payment.method",
                        "action_clover_check_status",
                        [this.payment_method.id, externalId]
                    );

                    if (res && res.state === "success") {
                        this._clearPolling(cid);
                        line.transaction_id = res.clover_payment_id || externalId;
                        line.card_type = res.card_brand || "Card";
                        if (res.card_last4) {
                            line.card_last4 = res.card_last4;
                        }
                        line.set_payment_status("done");
                        resolve(true);
                        return;
                    } else if (res && (res.state === "failed" || res.state === "cancelled")) {
                        this._clearPolling(cid);
                        line.set_payment_status("retry");
                        this._showError(res.error || _t("Payment declined or cancelled on the terminal."));
                        resolve(false);
                        return;
                    }
                } catch (err) {
                    console.warn("Clover poll status glitch:", err);
                }

                const timeoutId = setTimeout(checkStatus, 1500);
                this.pollTimeouts.set(cid, timeoutId);
            };

            const timeoutId = setTimeout(checkStatus, 1000);
            this.pollTimeouts.set(cid, timeoutId);
        });
    }

    /**
     * Sends cancellation signal to Clover terminal to clear customer screen.
     */
    async send_payment_cancel(order, cid) {
        return this.sendPaymentCancel(order, cid);
    }

    async sendPaymentCancel(order, cid) {
        if (super.sendPaymentCancel) {
            await super.sendPaymentCancel(order, cid);
        } else if (super.send_payment_cancel) {
            await super.send_payment_cancel(order, cid);
        }

        this._clearPolling(cid);

        const line = order ? order.get_paymentline(cid) : null;
        const externalId = line ? line.terminal_service_id : null;

        if (externalId) {
            try {
                await this.env.services.orm.silent.call(
                    "pos.payment.method",
                    "action_clover_cancel_payment",
                    [this.payment_method.id, externalId]
                );
            } catch (err) {
                console.warn("Could not cancel Clover payment:", err);
            }
        }

        if (line) {
            line.set_payment_status("retry");
        }
        return true;
    }

    _clearPolling(cid) {
        if (this.pollTimeouts.has(cid)) {
            clearTimeout(this.pollTimeouts.get(cid));
            this.pollTimeouts.delete(cid);
        }
    }

    _showError(message) {
        if (this.env.services.notification) {
            this.env.services.notification.add(message, {
                title: _t("Clover Terminal"),
                type: "danger",
                sticky: false,
            });
        }
    }
}

// Odoo 20 standard payment interface registry
registry.category("pos_payment_interfaces").add("clover_cloud", PaymentCloverCloud);
