# PCI-DSS Compliance Attestation & Architecture Mapping

**Module:** `pos_clover_cloud`  
**Target Standard:** Payment Card Industry Data Security Standard (PCI-DSS) v4.0  
**Integration Type:** Semi-Integrated Point of Sale via Clover Cloud Pay Display  
**Scope Assessment:** Qualifies merchant for **PCI-DSS SAQ A** / **SAQ A-EP** (Scope Minimization)

---

## 1. Core Architectural Principle

The fundamental security principle of `pos_clover_cloud` is **Complete Cardholder Data Isolation**:

```text
+-----------------------+              +------------------------+
|      Odoo POS         |              |   Clover Flex / Mini   |
|  (Server & Browser)   |              |   (PCI PTS Certified)  |
|                       |              |                        |
| - NO Card Data        |              | - Card Tap / Chip / PIN|
| - NO Track Data       |              | - Hardware Encryption  |
| - NO CVV / PIN        |              | - Direct Acquirer Sync |
+-----------+-----------+              +-----------+------------+
            |                                      |
            |   TLS 1.3 REST (Metadata Only)       |
            +------------------> Clover <----------+
                                 Cloud
```

1. **Zero Cardholder Data (CHD):** Under no circumstances does Odoo POS (frontend browser or backend PostgreSQL database) read, transmit, receive, or store Primary Account Numbers (PANs), cardholder names, expiration dates, or service codes.
2. **Zero Sensitive Authentication Data (SAD):** Magnetic stripe track data, CVV/CVC/CID codes, and PINs/PIN blocks are handled exclusively within the tamper-resistant hardware security module (HSM) of the Clover terminal.
3. **Out-of-Scope Card Processing:** All card transactions occur on PCI PTS (PIN Transaction Security) certified Clover hardware.

---

## 2. Detailed PCI-DSS v4.0 Requirement Mapping

### Requirement 3: Protect Stored Account Data
* **Implementation:** The `clover.transaction` model only stores:
  - An idempotency UUID v4 (`external_payment_id`).
  - An approval authorization code (`auth_code`).
  - The card brand (e.g. Visa, Mastercard).
  - Truncated/masked card number consisting strictly of the **last four digits** (`card_last4`).
* **Verification:** Full 16-digit PANs and CVVs are rejected and never mapped to any ORM field.

### Requirement 4: Protect Cardholder Data with Strong Cryptography During Transmission
* **Implementation:** All network calls between Odoo and Clover Cloud are enforced over HTTPS / TLS 1.3 with validated certificates.
* **Backend Isolation:** Client credentials (API Token & Merchant ID) remain strictly on the Odoo server and are never pushed to the POS browser client, preventing token exfiltration via client-side script injection.

### Requirement 8: Identify Users and Authenticate Access to System Components
* **Implementation:** 
  - Access control list (`ir.model.access.csv`) enforces strict least-privilege principles.
  - Cashiers (`group_pos_user`) can only initiate transactions through the POS wizard; they cannot manually alter or delete payment audit logs.
  - Audit logs (`clover.transaction`) are strictly read-only (`create="false" edit="false" delete="false"`) across the UI.

### Requirement 10: Log and Monitor All Access to System Components
* **Implementation:** Every payment attempt generates an immutable `clover.transaction` record capturing the timestamp, originating terminal, operator company, status, and Clover reference ID.
* **PCI Log Sanitizer:** A recursive sanitization engine (`sanitize_data_for_logs`) inspects all payloads prior to writing to Odoo server logs, automatically redacting authorization headers, tokens, and any potential cardholder metadata.
