import os
from pathlib import Path
import unittest
from unittest import mock

import harness


class HarnessTests(unittest.TestCase):
  def test_local_reasoning_effort_defaults_high_and_allows_override(self):
    with mock.patch.dict(os.environ, {}, clear=False):
      os.environ.pop("AEGIS_LOCAL_THINKING", None)
      self.assertEqual(harness.local_reasoning_effort(), "high")
    with mock.patch.dict(os.environ, {"AEGIS_LOCAL_THINKING": "max"}):
      self.assertEqual(harness.local_reasoning_effort(), "max")
    with mock.patch.dict(os.environ, {"AEGIS_LOCAL_THINKING": "invalid"}):
      self.assertEqual(harness.local_reasoning_effort(), "high")

  def test_image_attachment_note_preserves_task_order(self):
    staged = [
        ("input/A-101.png", Path("/tmp/000.png")),
        ("input/M-101.png", Path("/tmp/001.png")),
    ]
    note = harness._image_attachment_note(staged)
    self.assertIn("1. input/A-101.png", note)
    self.assertIn("2. input/M-101.png", note)

  def test_ifc_data_hall_contract_requires_canonical_route_signatures(self):
    instruction = "Build an IFC4 data hall with cable tray and busway routes."
    note = harness._task_contract_note(instruction)
    self.assertIn("IfcCableSegment/BUSBARSEGMENT", note)
    self.assertIn("IfcPipeSegment/RIGIDSEGMENT", note)
    self.assertIn("IfcElectricDistributionBoard/DISTRIBUTIONBOARD", note)
    self.assertIn("face-touching endpoints", note)
    self.assertIn("same-service overlaps are hard clashes", note)

  def test_ifc_contract_note_is_not_added_to_other_tasks(self):
    self.assertEqual(harness._task_contract_note("Produce a CSV report."), "")


if __name__ == "__main__":
  unittest.main()
