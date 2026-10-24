# -*- coding: utf-8 -*-
"""
Clover POS Gateway Controllers.
Provides secure endpoints for status querying and Clover webhooks.
"""

import json
import logging
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class CloverPosController(http.Controller):

    @http.route("/pos_clover/health", type="http", auth="public", methods=["GET"], csrf=False)
    def clover_health_check(self):
        """Simple health check endpoint to verify module routing."""
        return request.make_response(
            json.dumps({"status": "ok", "service": "pos_clover_cloud"}),
            headers=[("Content-Type", "application/json")]
        )
