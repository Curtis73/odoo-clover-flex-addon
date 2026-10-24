# -*- coding: utf-8 -*-
"""
Clover POS Gateway Controllers.
Provides secure endpoints for status querying and Clover webhooks.
"""

import json
import logging

try:
    from odoo import http
    from odoo.http import request
except ImportError:
    class _DummyHttp:
        def route(self, *args, **kwargs):
            return lambda f: f
        Controller = object
    http = _DummyHttp()
    request = None

_logger = logging.getLogger(__name__)


class CloverPosController(http.Controller):

    @http.route("/pos_clover/health", type="http", auth="public", methods=["GET"], csrf=False)
    def clover_health_check(self):
        """Simple health check endpoint to verify module routing."""
        if request:
            return request.make_response(
                json.dumps({"status": "ok", "service": "pos_clover_cloud"}),
                headers=[("Content-Type", "application/json")]
            )
        return json.dumps({"status": "ok", "service": "pos_clover_cloud"})
