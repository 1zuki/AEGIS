import unittest

import aegis_gates


class ConstraintGateTests(unittest.TestCase):
  def test_recomputes_pipe_metric_before_comparison(self):
    report = aegis_gates.build_report(
        constraint_payload={"constraints": [{
            "id": "head_loss_limit",
            "recompute": {
                "tool": "pipe_hydraulics",
                "parameters": {
                    "flow_m3_s": 0.01,
                    "diameter_m": 0.1,
                    "length_m": 100.0,
                    "roughness_m": 4.5e-5,
                },
                "metric": "total_head_loss_m",
            },
            "limit": 10.0,
            "limit_unit": "m",
            "relation": "<=",
            "hard": True,
        }]},
    )
    self.assertTrue(report.passed, report.summary())
    self.assertTrue(report.constraints[0].independently_recomputed)

  def test_model_reported_number_is_unverified(self):
    report = aegis_gates.build_report(
        constraint_payload={"constraints": [{
            "id": "claim",
            "actual": 1.0,
            "limit": 2.0,
            "relation": "<=",
            "hard": True,
        }]},
    )
    self.assertFalse(report.passed)
    self.assertEqual(report.unverified_constraints, 1)

  def test_rejects_reported_value_that_disagrees_with_recomputation(self):
    constraints, malformed = aegis_gates.evaluate_constraint_register({
        "constraints": [{
            "id": "mismatch",
            "actual": 999.0,
            "recompute": {
                "tool": "pipe_hydraulics",
                "parameters": {
                    "flow_m3_s": 0.01,
                    "diameter_m": 0.1,
                    "length_m": 100.0,
                    "roughness_m": 4.5e-5,
                },
                "metric": "total_head_loss_m",
            },
            "limit": 1000.0,
            "limit_unit": "m",
            "relation": "<=",
        }],
    })
    self.assertEqual(malformed, 1)
    self.assertEqual(constraints, ())

  def test_unsupported_metric_is_malformed(self):
    _, malformed = aegis_gates.evaluate_constraint_register({
        "constraints": [{
            "id": "bad_metric",
            "recompute": {
                "tool": "beam_elastic_screen",
                "parameters": {
                    "span_m": 4,
                    "uniform_load_n_m": 1000,
                    "elastic_modulus_pa": 200e9,
                    "inertia_m4": 8e-6,
                    "section_modulus_m3": 4e-5,
                    "allowable_bending_pa": 150e6,
                },
                "metric": "invention",
            },
            "limit": 1,
            "relation": "<=",
        }],
    })
    self.assertEqual(malformed, 1)

  def test_rejects_limit_unit_that_does_not_match_recomputed_metric(self):
    _, malformed = aegis_gates.evaluate_constraint_register({
        "constraints": [{
            "id": "wrong_unit",
            "recompute": {
                "tool": "pipe_hydraulics",
                "parameters": {
                    "flow_m3_s": 0.01,
                    "diameter_m": 0.1,
                    "length_m": 100.0,
                    "roughness_m": 4.5e-5,
                },
                "metric": "total_head_loss_m",
            },
            "limit": 15000,
            "limit_unit": "Pa",
            "relation": "<=",
        }],
    })
    self.assertEqual(malformed, 1)


class AuthorityGateTests(unittest.TestCase):
  def test_allows_one_current_and_superseded_revision(self):
    report = aegis_gates.build_report(
        authority_payload={
            "records": [
                {"group": "set", "path": "S-101-C.pdf", "revision": "C", "status": "current"},
                {"group": "set", "path": "S-101-B.pdf", "revision": "B", "status": "superseded"},
            ],
            "unresolved": [],
        },
        authority_present={"S-101-C.pdf": True, "S-101-B.pdf": True},
    )
    self.assertTrue(report.passed, report.summary())

  def test_blocks_multiple_current_revisions(self):
    authority = aegis_gates.evaluate_authority_register({
        "records": [
            {"group": "set", "path": "a.pdf", "revision": "A", "status": "current"},
            {"group": "set", "path": "b.pdf", "revision": "B", "status": "current"},
        ],
        "unresolved": [],
    })
    self.assertEqual(authority.conflict_groups, ("set",))
    self.assertFalse(authority.passed)

  def test_blocks_unresolved_conflicts_and_missing_sources(self):
    authority = aegis_gates.evaluate_authority_register(
        {
            "records": [{"group": "set", "path": "missing.pdf", "revision": "A", "status": "current"}],
            "unresolved": ["date conflict"],
        },
        {"missing.pdf": False},
    )
    self.assertEqual(authority.unresolved, 1)
    self.assertEqual(authority.missing_records, 1)
    self.assertFalse(authority.passed)


class DeliverableGateTests(unittest.TestCase):
  def test_checks_presence_suffix_and_size(self):
    result, malformed = aegis_gates.evaluate_deliverables(
        {"files": [
            {"path": "out/report.xlsx", "required": True, "format": "xlsx", "min_bytes": 10},
            {"path": "out/optional.csv", "required": False, "format": "csv"},
        ]},
        {"out/report.xlsx": (True, 100), "out/optional.csv": (False, 0)},
    )
    self.assertEqual(malformed, 0)
    self.assertTrue(result[0].passed)
    self.assertTrue(result[1].passed)

  def test_rejects_parent_traversal(self):
    _, malformed = aegis_gates.evaluate_deliverables(
        {"files": [{"path": "../outside.txt"}]},
        {},
    )
    self.assertEqual(malformed, 1)


if __name__ == "__main__":
  unittest.main()
