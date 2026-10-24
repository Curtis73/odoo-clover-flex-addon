/** @odoo-module */

import { PaymentInterface } from "@point_of_sale/app/payment/payment_interface";
import { register_payment_method } from "@point_of_sale/app/store/pos_store";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";

/**
 * Generates an RFC 4122 compliant UUID v4 for transaction idempotency.
 */
function generateUUID() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) {
        return crypto.randomUUID();
    }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
        const r = (Math.random() * 16) | 0;
        const v = c === "x" ? r : (r & 0x3) | 0x8;
        return v.toString(16);
    });
}

/**
 * Clover Cloud Pay Display Payment Interface for Odoo POS.
 * Connects POS OWL checkout with Clover Flex / Clover Mini terminals via secure backend proxy.
 * Fully compatible with Odoo 17, 18, 19, and 20+.
 */
export class PaymentCloverCloud extends PaymentInterface {
    setup() {
        super.setup(...arguments);
        this.pollTimeouts = new Map();
    }

    // Modern Odoo 18 / 20 POS camelCase method
    async sendPaymentRequest(cid) {
        return this.send_payment_request(cid);
    }

    // Classic / Odoo 17 POS snake_case method
    async send_payment_request(cid) {
        if (super.send_payment_request) {
            await super.send_payment_request(cid);
        } else if (super.sendPaymentRequest) {
            await super.sendPaymentRequest(cid);
        }

        const order = this.pos.get_order();
        const line = order ? (order.get_paymentline ? order.get_paymentline(cid) : order.paymentlines.find((l) => l.cid === cid)) : null;

        if (!line) {
            return false;
        }

        // Generate unique UUID v4 for payment idempotency
        const externalId = generateUUID();
        line.terminal_service_id = externalId;
        line.set_payment_status("waitingCard");

        // 1. Send payment initiation request to Odoo backend
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
            const errorMsg = (initResponse && initResponse.error) || _t("Could not contact Clover terminal.");
            this._showError(errorMsg);
            return false;
        }

        // Check if transaction was immediately approved (synchronous response)
        if (initResponse.state === "success") {
            line.transaction_id = initResponse.clover_payment_id || externalId;
            line.card_type = initResponse.card_brand || "Card";
            if (initResponse.card_last4) {
                line.card_last4 = initResponse.card_last4;
            }
            line.set_payment_status("done");
            return true;
        }

        // 2. Poll transaction state asynchronously (non-blocking)
        return this._pollPaymentStatus(cid, externalId);
    }

    /**
     * Polls backend for terminal status updates.
     */
    _pollPaymentStatus(cid, externalId) {
        return new Promise((resolve) => {
            const checkStatus = async () => {
                const order = this.pos.get_order();
                const line = order ? (order.get_paymentline ? order.get_paymentline(cid) : order.paymentlines.find((l) => l.cid === cid)) : null;

                // Stop polling if order or payment line was removed
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
                        const errorMsg = res.error || _t("Payment was declined or cancelled on the terminal.");
                        this._showError(errorMsg);
                        resolve(false);
                        return;
                    }
                } catch (err) {
                    console.warn("Clover poll status glitch:", err);
                }

                // Schedule next poll in 1500ms
                const timeoutId = setTimeout(checkStatus, 1500);
                this.pollTimeouts.set(cid, timeoutId);
            };

            // Start polling loop
            const timeoutId = setTimeout(checkStatus, 1000);
            this.pollTimeouts.set(cid, timeoutId);
        });
    }

    // Modern Odoo 18 / 20 POS camelCase cancel method
    async sendPaymentCancel(order, cid) {
        return this.send_payment_cancel(order, cid);
    }

    // Classic / Odoo 17 POS snake_case cancel method
    async send_payment_cancel(order, cid) {
        if (super.send_payment_cancel) {
            await super.send_payment_cancel(order, cid);
        } else if (super.sendPaymentCancel) {
            await super.sendPaymentCancel(order, cid);
        }

        this._clearPolling(cid);

        const line = order ? (order.get_paymentline ? order.get_paymentline(cid) : order.paymentlines.find((l) => l.cid === cid)) : null;
        const externalId = line ? line.terminal_service_id : null;

        if (externalId) {
            try {
                await this.env.services.orm.silent.call(
                    "pos.payment.method",
                    "action_clover_cancel_payment",
                    [this.payment_method.id, externalId]
                );
            } catch (err) {
                console.warn("Could not send cancel to Clover terminal:", err);
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
        } else if (this.pos.popup) {
            this.pos.popup.add("ErrorPopup", {
                title: _t("Clover Terminal Error"),
                body: message,
            });
        }
    }
}

// Dual registration for universal compatibility (Odoo 17, 18, 19, 20)
if (register_payment_method) {
    try {
        register_payment_method("clover_cloud", PaymentCloverCloud);
    } catch (e) {
        // Fallback to registry if register_payment_method is deprecated
    }
}

if (registry && registry.category) {
    const paymentInterfaces = registry.category("pos_payment_interfaces");
    if (paymentInterfaces && !paymentInterfaces.contains("clover_cloud")) {
        paymentInterfaces.add("clover_cloud", PaymentCloverCloud);
    }
}
