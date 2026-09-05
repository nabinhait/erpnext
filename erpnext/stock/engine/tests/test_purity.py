"""The purity gate (design doc R8): the engine may never touch a framework or I/O.

The source scan is the guard. (The old "importing the core must not load
frappe into sys.modules" check died with vendoring: under the bench test
runner frappe is always already loaded, and the engine now lives inside the
erpnext package whose __init__ imports it.)
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(
	r"^\s*(?:import|from)\s+(frappe|erpnext|requests|pymysql|psycopg2)\b", re.MULTILINE)


class TestPurity(unittest.TestCase):
	def test_engine_sources_never_import_a_framework(self) -> None:
		for path in sorted(ENGINE.glob("*.py")):
			match = FORBIDDEN.search(path.read_text())
			assert not match, f"{path.name} imports {match.group(1)}"
