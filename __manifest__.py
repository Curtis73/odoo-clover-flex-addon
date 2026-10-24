# -*- coding: utf-8 -*-
{
    "name": "Clover Cloud Pay POS Integration",
    "version": "20.0.1.0.0",
    "category": "Sales/Point of Sale",
    "sequence": 6,
    "summary": "High-performance, PCI-DSS compliant Clover Cloud Pay terminal integration for Odoo 20 POS",
    "description": """
Clover Cloud Pay Terminal Integration for Odoo 20 POS
=====================================================
Enterprise-grade semi-integrated payment terminal solution for Odoo 20 POS.
Connects directly to physical Clover Flex (Mobile/Handheld) and Clover Mini terminals
running the official Clover Cloud Pay Display application.

Architectural Features:
-----------------------
* Native Odoo 20 POS OWL interface registered via pos_payment_interfaces.
* Direct cloud communication via Clover /connect/v1 REST Pay Display API.
* PCI-DSS v4.0 SAQ-A compliant: Zero cardholder data or PAN enters Odoo.
* Complete double-charge prevention using UUID v4 idempotency keys.
* Connection pooling with keep-alive for sub-150ms transaction dispatch.
* Support for on-device customer tipping and thermal receipt printing.
    """,
    "author": "Open Source Community",
    "license": "LGPL-3",
    "depends": ["point_of_sale"],
    "data": [
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
