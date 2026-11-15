import json
import math
import subprocess
import sys
import unittest
from pathlib import Path

import engineering_calcs


ROOT = Path(__file__).resolve().parents[1]


class PipeHydraulicsTests(unittest.TestCase):
  def test_laminar_flow_uses_darcy_factor(self):
    result = engineering_calcs.pipe_hydraulics({
        "flow_m3_s": 1e-5,
        "diameter_m": 0.1,
        "length_m": 10.0,
        "roughness_m": 1e-5,
        "kinematic_viscosity_m2_s": 1e-6,
    })
    self.assertEqual(result["flow_regime"], "laminar")
    self.assertAlmostEqual(
        result["darcy_friction_factor"],
        64.0 / result["reynolds_number"],
    )

  def test_turbulent_factor_satisfies_colebrook_equation(self):
    result = engineering_calcs.pipe_hydraulics({
        "flow_m3_s": 0.01,
        "diameter_m": 0.1,
        "length_m": 100.0,
        "roughness_m": 4.5e-5,
    })
    reynolds = result["reynolds_number"]
    factor = result["darcy_friction_factor"]
    residual = 1.0 / math.sqrt(factor) + 2.0 * math.log10(
        result["relative_roughness"] / 3.7
        + 2.51 / (reynolds * math.sqrt(factor))
    )
    self.assertEqual(result["flow_regime"], "turbulent")
    self.assertAlmostEqual(residual, 0.0, places=11)

  def test_minor_loss_increases_total_head(self):
    base = {
        "flow_m3_s": 0.01,
        "diameter_m": 0.1,
        "length_m": 100.0,
        "roughness_m": 4.5e-5,
    }
    straight = engineering_calcs.pipe_hydraulics(base)
    fitted = engineering_calcs.pipe_hydraulics({**base, "minor_loss_k": 4.0})
    self.assertGreater(fitted["total_head_loss_m"], straight["total_head_loss_m"])
    self.assertAlmostEqual(
        fitted["minor_head_loss_m"],
        4.0 * fitted["velocity_m_s"] ** 2 / (2.0 * engineering_calcs.G),
    )

  def test_zero_flow_has_zero_losses(self):
    result = engineering_calcs.pipe_hydraulics({
        "flow_m3_s": 0.0,
        "diameter_m": 0.1,
        "length_m": 100.0,
        "roughness_m": 4.5e-5,
    })
    self.assertEqual(result["flow_regime"], "zero_flow")
    self.assertEqual(result["total_head_loss_m"], 0.0)

  def test_rejects_invalid_physical_inputs(self):
    with self.assertRaisesRegex(ValueError, "diameter_m"):
      engineering_calcs.pipe_hydraulics({
          "flow_m3_s": 0.01,
          "diameter_m": 0.0,
          "length_m": 100.0,
          "roughness_m": 1e-5,
      })


class BeamScreenTests(unittest.TestCase):
  def test_uniform_load_screen_matches_closed_form(self):
    result = engineering_calcs.beam_elastic_screen({
        "span_m": 4.0,
        "uniform_load_n_m": 1000.0,
        "elastic_modulus_pa": 200e9,
        "inertia_m4": 8e-6,
        "section_modulus_m3": 4e-5,
        "allowable_bending_pa": 150e6,
    })
    self.assertAlmostEqual(result["maximum_moment_n_m"], 2000.0)
    self.assertAlmostEqual(result["maximum_bending_stress_pa"], 50e6)
    self.assertTrue(result["screen_passed"])

  def test_screen_fails_bending_limit(self):
    result = engineering_calcs.beam_elastic_screen({
        "span_m": 4.0,
        "uniform_load_n_m": 10000.0,
        "elastic_modulus_pa": 200e9,
        "inertia_m4": 8e-6,
        "section_modulus_m3": 4e-5,
        "allowable_bending_pa": 150e6,
    })
    self.assertFalse(result["bending_check_passed"])
    self.assertFalse(result["screen_passed"])

  def test_rejects_zero_modulus(self):
    with self.assertRaisesRegex(ValueError, "elastic_modulus_pa"):
      engineering_calcs.beam_elastic_screen({
          "span_m": 4.0,
          "uniform_load_n_m": 1000.0,
          "elastic_modulus_pa": 0.0,
          "inertia_m4": 8e-6,
          "section_modulus_m3": 4e-5,
          "allowable_bending_pa": 150e6,
      })

  def test_json_cli_dispatches_tool(self):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "engineering_calcs.py")],
        input=json.dumps({
            "tool": "pipe_hydraulics",
            "parameters": {
                "flow_m3_s": 0.01,
                "diameter_m": 0.1,
                "length_m": 100.0,
                "roughness_m": 4.5e-5,
            },
        }),
        text=True,
        capture_output=True,
        check=True,
    )
    response = json.loads(completed.stdout)
    self.assertTrue(response["ok"])
    self.assertGreater(response["result"]["total_head_loss_m"], 0)


if __name__ == "__main__":
  unittest.main()
