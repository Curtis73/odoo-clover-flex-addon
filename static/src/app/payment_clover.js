/** @odoo-module */

import { PaymentInterface } from "@point_of_sale/app/utils/payment/payment_interface";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
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
     * Resolves the numeric pos.payment.method ID across all Odoo POS versions.
     */
    getPaymentMethodId(line) {
        if (line && line.payment_method_id) {
            return typeof line.payment_method_id === "object"
                ? line.payment_method_id.id
                : line.payment_method_id;
        }
        if (this.payment_method_id) {
            return typeof this.payment_method_id === "object"
                ? this.payment_method_id.id
                : this.payment_method_id;
        }
        if (this.payment_method) {
            return typeof this.payment_method === "object"
                ? this.payment_method.id
                : this.payment_method;
        }
        return null;
    }

    /**
     * Sets payment line status supporting both Odoo 20 (setPaymentStatus)
     * and legacy Odoo (set_payment_status).
     */
    _setPaymentStatus(line, status) {
        if (!line) {
            return;
        }
        if (typeof line.setPaymentStatus === "function") {
            line.setPaymentStatus(status);
        } else if (typeof line.set_payment_status === "function") {
            line.set_payment_status(status);
        }
    }

    /**
     * Wraps calls to pos.payment.method actions.
     */
    async _callCloverAction(method, params) {
        if (typeof this.callPaymentMethod === "function") {
            return await this.callPaymentMethod(method, params);
        }
        return await this.env.services.orm.silent.call("pos.payment.method", method, params);
    }

    /**
     * Polymorphic handler supporting both Odoo 20 (line object) and earlier (line CID).
     */
    async send_payment_request(arg) {
        return this.sendPaymentRequest(arg);
    }

    async sendPaymentRequest(arg) {
        if (super.sendPaymentRequest) {
            await super.sendPaymentRequest(...arguments);
        } else if (super.send_payment_request) {
            await super.send_payment_request(...arguments);
        }

        const line = (typeof arg === "object" && arg !== null)
            ? arg
            : (this.pos.get_order() ? this.pos.get_order().get_paymentline(arg) : null);

        if (!line) {
            return false;
        }

        const pmId = this.getPaymentMethodId(line);
        if (!pmId) {
            this._showError(_t("Could not identify payment method ID for Clover terminal."));
            return false;
        }

        const lineId = line.uuid || line.cid || "clover_line";

        // Robust UUID v4 generation compatible with older embedded POS browsers
        const externalId = (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function")
            ? crypto.randomUUID()
            : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
                const r = (Math.random() * 16) | 0;
                const v = c === "x" ? r : (r & 0x3) | 0x8;
                return v.toString(16);
            });
        line.terminal_service_id = externalId;
        this._setPaymentStatus(line, "waitingCard");

        let initResponse;
        try {
            initResponse = await this._callCloverAction(
                "action_clover_initiate_payment",
                [pmId, line.amount, externalId]
            );
        } catch (error) {
            this._setPaymentStatus(line, "retry");
            this._showError(_t("Failed to communicate with Odoo server: ") + (error.message || error));
            return false;
        }

        if (!initResponse || !initResponse.success) {
            this._setPaymentStatus(line, "retry");
            this._showError((initResponse && initResponse.error) || _t("Could not contact Clover terminal."));
            return false;
        }

        if (initResponse.state === "success") {
            line.transaction_id = initResponse.clover_payment_id || externalId;
            line.card_type = initResponse.card_brand || "Card";
            if (initResponse.card_last4) {
                line.card_last4 = initResponse.card_last4;
            }
            if (typeof line.setReceiptInfo === "function") {
                const info = `${initResponse.card_brand || "Card"} ****${initResponse.card_last4 || ""}\nAuth: ${initResponse.auth_code || "OK"}`;
                line.setReceiptInfo(info);
            }
            this._setPaymentStatus(line, "done");
            return true;
        }

        return this._pollPaymentStatus(line, externalId);
    }

    /**
     * Asynchronously polls Odoo backend for terminal transaction updates.
     */
    _pollPaymentStatus(line, externalId) {
        const lineKey = line.uuid || line.cid || externalId;
        const pmId = this.getPaymentMethodId(line);
        return new Promise((resolve) => {
            const checkStatus = async () => {
                if (!line || line.terminal_service_id !== externalId) {
                    this._clearPolling(lineKey);
                    resolve(false);
                    return;
                }

                try {
                    const res = await this._callCloverAction(
                        "action_clover_check_status",
                        [pmId, externalId]
                    );

                    if (res && res.state === "success") {
                        this._clearPolling(lineKey);
                        line.transaction_id = res.clover_payment_id || externalId;
                        line.card_type = res.card_brand || "Card";
                        if (res.card_last4) {
                            line.card_last4 = res.card_last4;
                        }
                        if (typeof line.setReceiptInfo === "function") {
                            const info = `${res.card_brand || "Card"} ****${res.card_last4 || ""}\nAuth: ${res.auth_code || "OK"}`;
                            line.setReceiptInfo(info);
                        }
                        this._setPaymentStatus(line, "done");
                        resolve(true);
                        return;
                    } else if (res && (res.state === "failed" || res.state === "cancelled")) {
                        this._clearPolling(lineKey);
                        this._setPaymentStatus(line, "retry");
                        this._showError(res.error || _t("Payment declined or cancelled on the terminal."));
                        resolve(false);
                        return;
                    }
                } catch (err) {
                    console.warn("Clover poll status glitch:", err);
                }

                const timeoutId = setTimeout(checkStatus, 1500);
                this.pollTimeouts.set(lineKey, timeoutId);
            };

            const timeoutId = setTimeout(checkStatus, 1000);
            this.pollTimeouts.set(lineKey, timeoutId);
        });
    }

    /**
     * Sends cancellation signal to Clover terminal to clear customer screen.
     */
    async send_payment_cancel(order, arg) {
        return this.sendPaymentCancel(order, arg);
    }

    async sendPaymentCancel(order, arg) {
        if (super.sendPaymentCancel) {
            await super.sendPaymentCancel(...arguments);
        } else if (super.send_payment_cancel) {
            await super.send_payment_cancel(...arguments);
        }

        const line = (typeof arg === "object" && arg !== null)
            ? arg
            : (order ? order.get_paymentline(arg) : null);

        const lineKey = line ? (line.uuid || line.cid || "cancel_key") : "cancel_key";
        this._clearPolling(lineKey);

        const externalId = line ? line.terminal_service_id : null;
        const pmId = this.getPaymentMethodId(line);
        if (externalId && pmId) {
            try {
                await this._callCloverAction(
                    "action_clover_cancel_payment",
                    [pmId, externalId]
                );
            } catch (err) {
                console.warn("Could not cancel Clover payment:", err);
            }
        }

        if (line) {
            this._setPaymentStatus(line, "retry");
        }
        return true;
    }

    _clearPolling(key) {
        if (this.pollTimeouts.has(key)) {
            clearTimeout(this.pollTimeouts.get(key));
            this.pollTimeouts.delete(key);
        }
    }

    _showError(message) {
        const dialog = this.dialog || this.env?.services?.dialog;
        if (dialog) {
            dialog.add(AlertDialog, {
                title: _t("Clover Terminal"),
                body: message,
            });
        } else if (this.notification || this.env?.services?.notification) {
            const notif = this.notification || this.env?.services?.notification;
            notif.add(message, {
                title: _t("Clover Terminal"),
                type: "danger",
                sticky: false,
            });
        }
    }
}

// Register for Odoo 20 pos_payment_providers registry
registry.category("pos_payment_providers").add("clover_cloud", PaymentCloverCloud);
// Also register under pos_payment_interfaces for backwards compatibility
if (registry.category("pos_payment_interfaces")) {
    registry.category("pos_payment_interfaces").add("clover_cloud", PaymentCloverCloud);
}
