#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Convenience test runner for pos_clover_cloud.
Executes all unit tests with Odoo test harness preloaded.
"""

import os
import sys
import unittest

if __name__ == "__main__":
    current_dir = os.path.dirname(os.path.abspath(__file__))
    tests_dir = os.path.join(current_dir, "tests")
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)

    loader = unittest.TestLoader()
    suite = loader.discover(tests_dir, pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(not result.wasSuccessful())
