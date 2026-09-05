from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LicensingTests(unittest.TestCase):
    def test_project_license_metadata_is_consistent(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        frontend = json.loads((ROOT / "frontend/package.json").read_text())
        locked = json.loads((ROOT / "frontend/package-lock.json").read_text())["packages"][""]
        self.assertEqual("Apache-2.0", project["license"])
        self.assertEqual(project["license"], frontend["license"])
        self.assertEqual(frontend["license"], locked["license"])
        self.assertEqual(
            {"LICENSE", "NOTICE", "THIRD_PARTY_NOTICES"}, set(project["license-files"])
        )
        for filename in project["license-files"]:
            self.assertTrue((ROOT / filename).read_text().strip())

    def test_apache_terms_and_upstream_attribution_are_present(self):
        license_text = (ROOT / "LICENSE").read_text()
        self.assertIn("Version 2.0, January 2004", license_text)
        self.assertIn("3. Grant of Patent License.", license_text)
        notices = (ROOT / "THIRD_PARTY_NOTICES").read_text()
        self.assertIn("Meta Platforms, Inc. and affiliates.", notices)
        self.assertIn("Lucide Contributors 2022.", notices)
