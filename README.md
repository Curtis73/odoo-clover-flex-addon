# Clover Cloud Pay Integration for Odoo Point of Sale (`pos_clover_cloud`)

An open-source, enterprise-grade, PCI-DSS compliant payment integration connecting **Odoo POS** with physical **Clover Flex** and **Clover Mini** (generations 1, 2, and 3) terminals via Clover's **Cloud Pay Display REST API (v3)**.

Eliminates the need for £200+ paid third-party extensions with a clean, high-performance, and secure in-house architecture.

---

## Key Features

- **Direct Cloud Integration:** No local network cables, Raspberry Pi bridges, or proprietary middlemen required.
- **PCI-DSS Compliant by Design:** Strict semi-integrated architecture. Zero card numbers (PAN), CVVs, or PINs ever enter Odoo.
- **Double-Charge Protection:** Full UUID v4 idempotency keys ensure network drops or tab refreshes never duplicate a charge.
- **Non-Blocking Asynchronous UI:** Built using modern Odoo Web Library (OWL) and ES module standards.
- **Hardware Features Supported:**
  - On-terminal customer tip prompting.
  - Receipt printing directly on the Clover Flex built-in thermal printer.
  - Multi-company and multi-terminal support.
  - Full transaction audit log and diagnostic tools.

---

## 1. Terminal Hardware Setup (Clover Flex / Mini)

1. Connect your Clover Flex or Mini to Wi-Fi or 4G LTE.
2. Open the **App Market** on your Clover device.
3. Search for and install the official free **Cloud Pay Display** app.
4. Launch the **Cloud Pay Display** app:
   - The screen will display: *"Cloud Pay Display is active and waiting for transactions"*.
   - Keep this app running on the terminal during business hours.
5. Retrieve your device's **Serial Number**:
   - Printed on the physical barcode label on the back/bottom of the terminal.
   - Alternatively, open **Settings > About Device > Status** on the terminal (e.g., `C031UQ52340015`).

---

## 2. Clover Merchant & API Configuration

1. Log into your **Clover Merchant Dashboard** (or [Clover Developer Sandbox](https://sandbox.dev.clover.com)):
   - **UK / Europe:** `https://www.clover.com/` (or `https://api.eu.clover.com`)
   - **US / Canada:** `https://www.clover.com/` (or `https://api.clover.com`)
2. Navigate to **Account & Setup > API Tokens** (or **Developer Settings** in sandbox).
3. Generate a new API token with **Ecommerce / Payments (Read & Write)** permissions.
4. Copy your:
   - **Merchant ID (mId)** (13 alphanumeric characters, visible in the URL or upper right corner).
   - **API Token** (Bearer token).

---

## 3. Odoo Installation & Configuration

### A. Add Module to Odoo Addons Path
Place the `pos_clover_cloud` folder inside your Odoo `addons` directory or your custom addons path:
```bash
# Addons path example
odoo-bin -c /etc/odoo.conf -u pos_clover_cloud
```

### B. Register Your Clover Terminal in Odoo
1. In Odoo, navigate to **Point of Sale > Configuration > Clover Terminals**.
2. Click **New**:
   - **Terminal Name:** e.g., `Main Bar - Clover Flex`
   - **Device Serial Number:** Enter your Clover device serial (e.g. `C031UQ52340015`).
   - **Hardware Model:** Select `Clover Flex` or `Clover Mini`.
3. Save the record.

### C. Configure POS Payment Method
1. Navigate to **Point of Sale > Configuration > Payment Methods**.
2. Click **New**:
   - **Name:** e.g., `Clover Card Terminal`
   - **Use a Payment Terminal:** Select **Clover Cloud Pay (Flex / Mini)**.
   - **Clover Terminal:** Select the terminal created in Step B.
   - **Clover Environment:** Select `Production UK / Europe`, `Production US / Canada`, or `Sandbox`.
   - **Merchant ID:** Paste your Clover Merchant ID.
   - **API Token:** Paste your Bearer API token.
   - **Customer Tip on Device:** Enable if you want the Clover terminal to prompt for tips.
   - **Print on Clover Terminal:** Enable to print card receipts on the Clover Flex printer.
3. Save the record.

### D. Link Payment Method to your POS
1. Navigate to **Point of Sale > Configuration > Settings**.
2. Select your POS shop / restaurant session.
3. Under **Payment Methods**, ensure `Clover Card Terminal` is checked.
4. Click **Save**.

---

## 4. Cashier Checkout Workflow

1. Open the POS Session.
2. Ring up order items and click **Payment**.
3. Select the **Clover Card Terminal** payment method.
4. The payment line will transition to **Waiting for Customer**:
   - The Clover Flex / Mini immediately lights up with the sale amount.
   - The customer inserts, taps, or swipes their card and enters their PIN (or adds a tip).
5. Upon bank authorization:
   - The Clover terminal chimes and displays **Approved**.
   - The Odoo POS screen marks the line as **Done** with the card brand and authorization code.
   - Cashier clicks **Validate** to finalize the order.

---

## 5. Running Automated Tests

To run the unit tests independently:
```bash
python -m unittest tests/test_clover_api.py
```

All 4 test suites verify PCI-DSS payload sanitization, idempotency keys, and error handling.
