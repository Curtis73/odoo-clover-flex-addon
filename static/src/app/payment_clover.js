/** @odoo-module */

import { PaymentInterface } from "@point_of_sale/app/utils/payment/payment_interface";
import { PosPayment } from "@point_of_sale/app/models/pos_payment";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import OrderPaymentValidation from "@point_of_sale/app/utils/order_payment_validation";
import { AlertDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

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
        } else {
            line.payment_status = status;
        }
    }

    /**
     * Resolves the PosPayment line from varying Odoo version call signatures.
     * In Odoo 20+, cancelPayment/sendPaymentRequest passes `this` (the line itself) as primary arg.
     * In earlier Odoo versions, (order, cid_or_line) or (cid) was passed.
     */
    _resolvePaymentLine(...args) {
        const [arg1, arg2] = args;

        // 1. Direct line object check
        if (arg1 && typeof arg1 === "object" && (arg1.payment_method_id || arg1.terminal_service_id || arg1.amount !== undefined)) {
            return arg1;
        }
        if (arg2 && typeof arg2 === "object" && (arg2.payment_method_id || arg2.terminal_service_id || arg2.amount !== undefined)) {
            return arg2;
        }

        // 2. Identify possible UUID/CID string
        const identifier = (typeof arg2 === "string" ? arg2 : (typeof arg1 === "string" ? arg1 : null));

        // 3. Check order
        const currentOrder = (arg1 && typeof arg1 === "object" && !arg1.payment_method_id)
            ? arg1
            : (this.pos ? (typeof this.pos.get_order === "function" ? this.pos.get_order() : this.pos.selectedOrder) : null);

        if (currentOrder) {
            if (identifier && typeof currentOrder.getPaymentlineByUuid === "function") {
                const found = currentOrder.getPaymentlineByUuid(identifier);
                if (found) return found;
            }
            if (identifier && typeof currentOrder.get_paymentline === "function") {
                try {
                    const found = currentOrder.get_paymentline(identifier);
                    if (found) return found;
                } catch (_) {}
            }
            const lines = currentOrder.payment_ids || currentOrder.paymentlines || [];
            if (Array.isArray(lines)) {
                if (identifier) {
                    const found = lines.find((p) => p.uuid === identifier || p.cid === identifier);
                    if (found) return found;
                }
                const active = lines.find((p) => p.payment_status === "waitingCard" || p.payment_status === "waiting");
                if (active) return active;
            }
            if (currentOrder.selected_paymentline) {
                return currentOrder.selected_paymentline;
            }
        }

        return (arg1 && typeof arg1 === "object") ? arg1 : (arg2 && typeof arg2 === "object" ? arg2 : null);
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
    async send_payment_request(...args) {
        return this.sendPaymentRequest(...args);
    }

    async sendPaymentRequest(...args) {
        if (super.sendPaymentRequest) {
            await super.sendPaymentRequest(...args);
        } else if (super.send_payment_request) {
            await super.send_payment_request(...args);
        }

        const line = this._resolvePaymentLine(...args);

        if (!line) {
            return false;
        }

        const pmId = this.getPaymentMethodId(line);
        if (!pmId) {
            this._showError(_t("Could not identify payment method ID for Clover terminal."));
            return false;
        }

        const lineId = line.uuid || line.cid || "clover_line";

        // Robust UUID v4 generation compatible with older embedded POS browsers (max 32 chars for Clover)
        const rawUuid = (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function")
            ? crypto.randomUUID()
            : "xxxxxxxxxxxx4xxxyxxxxxxxxxxxxxxx".replace(/[xy]/g, function (c) {
                const r = (Math.random() * 16) | 0;
                const v = c === "x" ? r : (r & 0x3) | 0x8;
                return v.toString(16);
            });
        const externalId = rawUuid.replace(/-/g, "").slice(0, 32);
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
    async send_payment_cancel(...args) {
        return this.sendPaymentCancel(...args);
    }

    async sendPaymentCancel(...args) {
        if (super.sendPaymentCancel) {
            await super.sendPaymentCancel(...args);
        } else if (super.send_payment_cancel) {
            await super.send_payment_cancel(...args);
        }

        const line = this._resolvePaymentLine(...args);

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
registry.category("pos_payment_providers").add("clover_cloud", PaymentCloverCloud, { force: true });
if (registry.category("pos_payment_interfaces")) {
    registry.category("pos_payment_interfaces").add("clover_cloud", PaymentCloverCloud, { force: true });
}

/**
 * Atomic Enforcement: Ensure Clover terminal interface is ALWAYS active
 * and the 'Send' button is ALWAYS displayed when Card is selected.
 */
patch(PaymentScreen.prototype, {
    async addNewPaymentLine(paymentMethod, args = {}) {
        const isClover =
            paymentMethod?.payment_provider === "clover_cloud" ||
            paymentMethod?.use_payment_terminal === "clover_cloud";

        if (isClover && !paymentMethod.payment_interface) {
            paymentMethod.payment_interface = new PaymentCloverCloud(this.pos, paymentMethod);
        }

        const res = await super.addNewPaymentLine(...arguments);

        if (isClover) {
            const line = this.selectedPaymentLine || this.paymentLines.at(-1);
            if (line && line.payment_status !== "done") {
                if (typeof line.setPaymentStatus === "function") {
                    line.setPaymentStatus("pending");
                } else if (typeof line.set_payment_status === "function") {
                    line.set_payment_status("pending");
                }
            }
        }
        return res;
    },
});

/**
 * Atomic Enforcement: A Clover payment line is NEVER done until the terminal
 * returns approval and sets status to 'done'.
 */
patch(PosPayment.prototype, {
    isDone() {
        const isClover =
            this.payment_method_id?.payment_provider === "clover_cloud" ||
            this.payment_method_id?.use_payment_terminal === "clover_cloud";
        if (isClover) {
            return this.payment_status === "done";
        }
        return super.isDone(...arguments);
    },
});

/**
 * Atomic Enforcement: Strictly block order validation if any Clover payment line
 * has not been authorized on the physical terminal.
 */
patch(OrderPaymentValidation.prototype, {
    async validateOrder(isForceValidate) {
        for (const line of this.paymentLines) {
            const isClover =
                line.payment_method_id?.payment_provider === "clover_cloud" ||
                line.payment_method_id?.use_payment_terminal === "clover_cloud";
            if (isClover && line.payment_status !== "done") {
                this.pos.dialog.add(AlertDialog, {
                    title: _t("Card Payment Required"),
                    body: _t(
                        "Cannot validate sale: The £%s card payment has not been taken on the Clover terminal.\n\n" +
                        "Please click 'Send' on the payment line and have the customer tap or insert their card on the terminal.",
                        this.pos.formatCurrency(line.amount)
                    ),
                });
                return false;
            }
        }
        return await super.validateOrder(...arguments);
    },
});
