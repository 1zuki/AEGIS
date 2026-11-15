# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic first-pass engineering calculations used by AEGIS tools.

The calculations are deliberately limited to declared idealized models. Their
results support engineering work; they do not establish code compliance,
professional approval, or suitability for construction.
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any, Mapping


G = 9.80665
WATER_DENSITY_20C = 998.2
WATER_KINEMATIC_VISCOSITY_20C = 1.004e-6


def _finite_number(data: Mapping[str, Any], key: str, *, default: float | None = None) -> float:
  value = data.get(key, default)
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise ValueError(f"{key} must be numeric")
  number = float(value)
  if not math.isfinite(number):
    raise ValueError(f"{key} must be finite")
  return number


def _positive(data: Mapping[str, Any], key: str, *, default: float | None = None) -> float:
  value = _finite_number(data, key, default=default)
  if value <= 0:
    raise ValueError(f"{key} must be greater than zero")
  return value


def _nonnegative(data: Mapping[str, Any], key: str, *, default: float = 0.0) -> float:
  value = _finite_number(data, key, default=default)
  if value < 0:
    raise ValueError(f"{key} must be non-negative")
  return value


def _darcy_friction_factor(reynolds: float, relative_roughness: float) -> tuple[float, str]:
  if reynolds == 0:
    return 0.0, "zero_flow"
  if reynolds < 2300:
    return 64.0 / reynolds, "laminar"

  # Colebrook-White fixed point, initialized with the explicit Swamee-Jain
  # approximation. The reported turbulent result is flagged in transition.
  term = relative_roughness / 3.7 + 5.74 / reynolds**0.9
  factor = 0.25 / (math.log10(term) ** 2)
  for _ in range(100):
    inverse_root = -2.0 * math.log10(
        relative_roughness / 3.7 + 2.51 / (reynolds * math.sqrt(factor))
    )
    updated = 1.0 / (inverse_root * inverse_root)
    if abs(updated - factor) <= 1e-13:
      factor = updated
      break
    factor = updated
  else:
    raise ArithmeticError("Colebrook iteration did not converge")
  regime = "transitional" if reynolds < 4000 else "turbulent"
  return factor, regime


def pipe_hydraulics(data: Mapping[str, Any]) -> dict[str, Any]:
  """Computes circular-pipe Darcy-Weisbach losses using SI units.

  Required inputs: flow_m3_s, diameter_m, length_m, roughness_m. Optional:
  kinematic_viscosity_m2_s (20 C water default), density_kg_m3 (20 C water
  default), minor_loss_k (0), and gravity_m_s2 (standard gravity).
  """
  flow = _nonnegative(data, "flow_m3_s")
  diameter = _positive(data, "diameter_m")
  length = _nonnegative(data, "length_m")
  roughness = _nonnegative(data, "roughness_m")
  viscosity = _positive(
      data,
      "kinematic_viscosity_m2_s",
      default=WATER_KINEMATIC_VISCOSITY_20C,
  )
  density = _positive(data, "density_kg_m3", default=WATER_DENSITY_20C)
  minor_k = _nonnegative(data, "minor_loss_k")
  gravity = _positive(data, "gravity_m_s2", default=G)
  if roughness >= diameter:
    raise ValueError("roughness_m must be less than diameter_m")

  area = math.pi * diameter**2 / 4.0
  velocity = flow / area
  reynolds = velocity * diameter / viscosity
  relative_roughness = roughness / diameter
  friction, regime = _darcy_friction_factor(reynolds, relative_roughness)
  velocity_head = velocity**2 / (2.0 * gravity)
  major_loss = friction * (length / diameter) * velocity_head
  minor_loss = minor_k * velocity_head
  total_loss = major_loss + minor_loss
  warnings: list[str] = []
  if regime == "transitional":
    warnings.append(
        "Reynolds number is transitional; friction factor is uncertain."
    )
  return {
      "model": "Darcy-Weisbach with Colebrook-White turbulent friction",
      "units": "SI",
      "velocity_m_s": velocity,
      "reynolds_number": reynolds,
      "relative_roughness": relative_roughness,
      "flow_regime": regime,
      "darcy_friction_factor": friction,
      "major_head_loss_m": major_loss,
      "minor_head_loss_m": minor_loss,
      "total_head_loss_m": total_loss,
      "pressure_drop_pa": density * gravity * total_loss,
      "metric_units": {
          "velocity_m_s": "m/s",
          "reynolds_number": "1",
          "relative_roughness": "1",
          "darcy_friction_factor": "1",
          "major_head_loss_m": "m",
          "minor_head_loss_m": "m",
          "total_head_loss_m": "m",
          "pressure_drop_pa": "Pa",
      },
      "warnings": warnings,
      "limitations": [
          "Steady, incompressible, single-phase flow in a full circular pipe.",
          "Check fluid properties, fittings, entrance/exit losses, and operating conditions against source documents.",
          "Transitional flow has significant friction-factor uncertainty.",
      ],
  }


def beam_elastic_screen(data: Mapping[str, Any]) -> dict[str, Any]:
  """Screens an elastic simply supported beam under uniform load in SI units.

  Inputs: span_m, uniform_load_n_m, elastic_modulus_pa, inertia_m4,
  section_modulus_m3, allowable_bending_pa, and optional deflection_ratio
  (default L/360). This does not check a structural design code.
  """
  span = _positive(data, "span_m")
  load = _nonnegative(data, "uniform_load_n_m")
  modulus = _positive(data, "elastic_modulus_pa")
  inertia = _positive(data, "inertia_m4")
  section_modulus = _positive(data, "section_modulus_m3")
  allowable = _positive(data, "allowable_bending_pa")
  ratio = _positive(data, "deflection_ratio", default=360.0)

  moment = load * span**2 / 8.0
  stress = moment / section_modulus
  deflection = 5.0 * load * span**4 / (384.0 * modulus * inertia)
  allowable_deflection = span / ratio
  stress_utilization = stress / allowable
  deflection_utilization = deflection / allowable_deflection
  return {
      "model": "linear-elastic simply supported beam under uniform load",
      "units": "SI",
      "maximum_moment_n_m": moment,
      "maximum_bending_stress_pa": stress,
      "maximum_deflection_m": deflection,
      "allowable_deflection_m": allowable_deflection,
      "stress_utilization": stress_utilization,
      "deflection_utilization": deflection_utilization,
      "metric_units": {
          "maximum_moment_n_m": "N*m",
          "maximum_bending_stress_pa": "Pa",
          "maximum_deflection_m": "m",
          "allowable_deflection_m": "m",
          "stress_utilization": "1",
          "deflection_utilization": "1",
      },
      "bending_check_passed": stress_utilization <= 1.0,
      "deflection_check_passed": deflection_utilization <= 1.0,
      "screen_passed": stress_utilization <= 1.0 and deflection_utilization <= 1.0,
      "limitations": [
          "Idealized prismatic, linearly elastic, simply supported beam with uniform load.",
          "Does not check shear, stability, local buckling, connections, vibration, load combinations, or structural code requirements.",
          "Allowable bending stress and deflection ratio must come from the governing project criteria.",
      ],
  }


_TOOLS = {
    "pipe_hydraulics": pipe_hydraulics,
    "beam_elastic_screen": beam_elastic_screen,
}


def calculate(tool: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
  """Runs one named calculation from validated numeric parameters."""
  if tool not in _TOOLS:
    raise ValueError("tool must be pipe_hydraulics or beam_elastic_screen")
  if not isinstance(parameters, Mapping):
    raise ValueError("parameters must be a JSON object")
  return _TOOLS[tool](parameters)


def dispatch(payload: Any) -> dict[str, Any]:
  if not isinstance(payload, Mapping):
    raise ValueError("input must be a JSON object")
  tool = payload.get("tool")
  params = payload.get("parameters")
  if not isinstance(params, Mapping):
    raise ValueError("parameters must be a JSON object")
  return calculate(tool, params)


def main() -> int:
  try:
    payload = json.loads(sys.stdin.read())
    result = {"ok": True, "result": dispatch(payload)}
  except (json.JSONDecodeError, TypeError, ValueError, ArithmeticError) as exc:
    result = {"ok": False, "error": str(exc)}
  print(json.dumps(result, allow_nan=False, separators=(",", ":")))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
