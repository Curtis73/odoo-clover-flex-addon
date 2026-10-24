# -*- coding: utf-8 -*-
{
    "name": "Clover Cloud Pay POS Integration (Flex & Mini)",
    "version": "1.0.0",
    "category": "Sales/Point of Sale",
    "sequence": 6,
    "summary": "Connect Clover Flex and Clover Mini payment terminals via Clover Cloud Pay Display",
    "description": """
Clover Cloud Pay Terminal Integration for Odoo Point of Sale
============================================================
Provides an open-source, PCI-DSS compliant semi-integrated payment terminal solution
for Odoo POS using physical Clover Flex and Clover Mini devices.

Key Features:
-------------
* Direct integration via Clover REST Pay Display API (v3).
* High performance with connection pooling and non-blocking asynchronous POS polling.
* Strict PCI-DSS compliance: Zero cardholder data or PAN stored in Odoo.
* Idempotency protection with client-generated UUIDs to eliminate double charges.
* Supports on-device customer tipping and thermal receipt printing.
* Works with Clover Flex (Mobile), Clover Mini 2, Clover Mini 3, and Clover Station.
* No £200+ vendor license fees or external proprietary bridge servers required.
    """,
    "author": "Open Source Community",
    "license": "LGPL-3",
    "depends": ["point_of_sale"],
    "data": [
        "security/clover_security.xml",
        "security/ir.model.access.csv",
        "views/clover_terminal_views.xml",
        "views/clover_transaction_views.xml",
        "views/pos_payment_method_views.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "pos_clover_cloud/static/src/app/payment_clover.js",
        ],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
