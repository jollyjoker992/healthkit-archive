"""The published FHIR terminology, examples and mapping document match the mapper."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from hka import fhir as F

REPO = Path(__file__).resolve().parents[2]
DOC = (REPO / "docs" / "fhir-mapping.md").read_text()


class Published(unittest.TestCase):
    def run_tool(self, name: str) -> None:
        out = subprocess.run([sys.executable, str(REPO / "tools" / name), "--check"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_terminology_files_are_generated_from_the_code(self):
        self.run_tool("fhir_terminology.py")

    def test_examples_are_generated_from_the_mapper(self):
        self.run_tool("fhir_examples.py")

    def test_terminology_carries_the_mapping_version(self):
        for res in F.terminology(F.DEFAULT_BASE):
            self.assertEqual(res["version"], F.MAPPING_VERSION, res["url"])
        self.assertIn(f"# FHIR mapping (version {F.MAPPING_VERSION})", DOC)

    def test_every_code_is_documented(self):
        for code, metric in F.METRICS.items():
            self.assertIn(f"`{code}`", DOC, code)
            self.assertIn(metric.display, DOC, code)
        for code in F.LOINC:
            self.assertRegex(DOC, rf"\b{re.escape(code)}\b", code)

    def test_a_different_base_moves_every_url(self):
        other = "https://example.org/fhir"
        text = json.dumps(F.terminology(other))
        self.assertNotIn(F.DEFAULT_BASE, text)
        self.assertIn(f"{other}/CodeSystem/hka-metrics", text)

    def test_metric_units_are_valid_for_their_value_type(self):
        for code, m in F.METRICS.items():
            self.assertIn(m.group, F.METRIC_GROUPS, code)
            self.assertEqual(m.unit is None, m.value_type == "CodeableConcept", code)


if __name__ == "__main__":
    unittest.main()
