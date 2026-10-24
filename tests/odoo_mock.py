# -*- coding: utf-8 -*-
"""
Standalone testing harness for pos_clover_cloud.
Injects a mock Odoo framework into sys.modules when running unit tests outside of an Odoo server.
Ensures zero test mock bloat in production model code.
"""

import sys
from unittest.mock import MagicMock


def setup_odoo_mock():
    if "odoo" in sys.modules:
        return

    odoo = MagicMock()
    odoo.api = MagicMock()
    odoo.fields = MagicMock()
    odoo.models = MagicMock()
    odoo.models.Model = object
    odoo._ = lambda s, *args: s if not args else s % args

    class UserError(Exception):
        pass

    class ValidationError(Exception):
        pass

    odoo.exceptions = MagicMock()
    odoo.exceptions.UserError = UserError
    odoo.exceptions.ValidationError = ValidationError

    odoo.http = MagicMock()
    odoo.http.Controller = object
    odoo.http.route = lambda *args, **kwargs: lambda f: f

    sys.modules["odoo"] = odoo
    sys.modules["odoo.api"] = odoo.api
    sys.modules["odoo.fields"] = odoo.fields
    sys.modules["odoo.models"] = odoo.models
    sys.modules["odoo.exceptions"] = odoo.exceptions
    sys.modules["odoo.http"] = odoo.http
