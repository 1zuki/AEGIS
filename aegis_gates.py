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

"""Small, deterministic postflight gates for an engineering task.

The model may leave two task-local JSON files in ``.aegis/``:

``constraint_register.json``
    ``{"constraints": [{"id": "...", "recompute": {"tool": "pipe_hydraulics",
    "parameters": {...}, "metric": "total_head_loss_m"}, "limit": 15.0,
    "limit_unit": "m", "relation": "<=", "hard": true, "source": "..."}]}``

``deliverables.json``
    ``{"files": [{"path": "report.xlsx", "required": true,
    "min_bytes": 1}]}``

``authority_register.json``
    ``{"records": [{"group": "structural_set", "path": "S-101.pdf",
    "revision": "C", "status": "current"}], "unresolved": []}``

The files are optional.  This module deliberately performs only checks that
are independent of the model: numeric comparisons, safe relative paths,
existence, file size, and optional suffix checks.  It does not infer an
engineering requirement or treat a model assertion as proof.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import PurePosixPath
from typing import Any, Mapping

import engineering_calcs


CONSTRAINT_REGISTER = ".aegis/constraint_register.json"
DELIVERABLE_MANIFEST = ".aegis/deliverables.json"
AUTHORITY_REGISTER = ".aegis/authority_register.json"
_RELATIONS = {"<", "<=", "==", ">=", ">"}
_AUTHORITY_STATUSES = {"current", "superseded", "reference", "not_used"}


@dataclass(frozen=True)
class ConstraintResult:
  """Result for one numeric constraint."""

  identifier: str
  passed: bool
  hard: bool
  relation: str
  actual: float
  limit: float
  independently_recomputed: bool


@dataclass(frozen=True)
class DeliverableResult:
  """Result for one declared deliverable."""

  path: str
  required: bool
  present: bool
  size: int
  suffix_ok: bool

  @property
  def passed(self) -> bool:
    return (not self.required or self.present) and self.suffix_ok


@dataclass(frozen=True)
class AuthorityRecord:
  """One source record selected or rejected by the agent."""

  group: str
  path: str
  revision: str
  status: str


@dataclass(frozen=True)
class AuthorityReport:
  """Result for source/revision bookkeeping."""

  records: tuple[AuthorityRecord, ...] = ()
  conflict_groups: tuple[str, ...] = ()
  missing_current_groups: tuple[str, ...] = ()
  missing_records: int = 0
  unresolved: int = 0
  malformed: int = 0

  @property
  def passed(self) -> bool:
    return not (
        self.conflict_groups
        or self.missing_current_groups
        or self.missing_records
        or self.unresolved
        or self.malformed
    )


@dataclass(frozen=True)
class GateReport:
  """Compact aggregate suitable for a task log or final response."""

  constraints: tuple[ConstraintResult, ...] = ()
  deliverables: tuple[DeliverableResult, ...] = ()
  authority: AuthorityReport | None = None
  malformed_constraints: int = 0
  malformed_deliverables: int = 0

  @property
  def hard_constraint_failures(self) -> int:
    return sum(1 for item in self.constraints if item.hard and not item.passed)

  @property
  def unverified_constraints(self) -> int:
    return sum(1 for item in self.constraints if not item.independently_recomputed)

  @property
  def required_deliverable_failures(self) -> int:
    return sum(
        1
        for item in self.deliverables
        if item.required and not item.passed
    )

  @property
  def passed(self) -> bool:
    return (
        self.hard_constraint_failures == 0
        and self.unverified_constraints == 0
        and self.required_deliverable_failures == 0
        and (self.authority is None or self.authority.passed)
        and self.malformed_constraints == 0
        and self.malformed_deliverables == 0
    )

  def summary(self) -> str:
    """Returns a content-minimal summary without task paths or values."""
    state = "CLEAR" if self.passed else "FLAGS"
    constraint_text = (
        f"constraints {len(self.constraints)} total, "
        f"{self.unverified_constraints} unverified, "
        f"{self.hard_constraint_failures} hard failures"
    )
    required = sum(1 for item in self.deliverables if item.required)
    deliverable_failures = self.required_deliverable_failures
    deliverable_text = (
        f"required deliverables {required} total, "
        f"{deliverable_failures} failures"
    )
    authority_text = ""
    if self.authority is not None:
      authority_text = (
          f"; authority records {len(self.authority.records)}"
          f", {len(self.authority.conflict_groups)} conflicts"
          f", {len(self.authority.missing_current_groups)} groups without a current revision"
          f", {self.authority.missing_records} missing"
          f", {self.authority.unresolved} unresolved"
      )
    malformed = self.malformed_constraints + self.malformed_deliverables
    if self.authority is not None:
      malformed += self.authority.malformed
    malformed_text = f"; malformed entries {malformed}" if malformed else ""
    return (
        f"AEGIS postflight: {state}; {constraint_text}; {deliverable_text}"
        f"{authority_text}{malformed_text}."
    )


def _number(value: Any, field: str) -> float:
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise ValueError(f"{field} must be numeric")
  value = float(value)
  if not math.isfinite(value):
    raise ValueError(f"{field} must be finite")
  return value


def _identifier(value: Any, field: str) -> str:
  if not isinstance(value, str) or not value.strip():
    raise ValueError(f"{field} must be a non-empty string")
  return value.strip()


def evaluate_constraint(raw: Mapping[str, Any]) -> ConstraintResult:
  """Evaluates a constraint, recomputing its actual from a supported tool."""
  identifier = _identifier(raw.get("id"), "id")
  limit = _number(raw.get("limit"), "limit")
  relation = raw.get("relation", "<=")
  if relation not in _RELATIONS:
    raise ValueError("relation must be one of <, <=, ==, >=, >")
  tolerance = _number(raw.get("tolerance", 0), "tolerance")
  if tolerance < 0:
    raise ValueError("tolerance must be non-negative")
  hard = raw.get("hard", True)
  if not isinstance(hard, bool):
    raise ValueError("hard must be boolean")

  recompute = raw.get("recompute")
  independently_recomputed = isinstance(recompute, Mapping)
  if independently_recomputed:
    tool = _identifier(recompute.get("tool"), "recompute.tool")
    parameters = recompute.get("parameters")
    if not isinstance(parameters, Mapping):
      raise ValueError("recompute.parameters must be an object")
    metric = _identifier(recompute.get("metric"), "recompute.metric")
    values = engineering_calcs.calculate(tool, parameters)
    actual = _number(values.get(metric), f"recompute.{metric}")
    metric_units = values.get("metric_units")
    expected_unit = (
        metric_units.get(metric)
        if isinstance(metric_units, Mapping)
        else None
    )
    limit_unit = _identifier(raw.get("limit_unit"), "limit_unit")
    if expected_unit is None or limit_unit != expected_unit:
      raise ValueError("limit_unit does not match the recomputed metric")
    if raw.get("actual") is not None:
      reported_actual = _number(raw.get("actual"), "actual")
      reported_tolerance = _number(
          raw.get("reported_actual_tolerance", 1e-6),
          "reported_actual_tolerance",
      )
      if reported_tolerance < 0 or abs(actual - reported_actual) > reported_tolerance:
        raise ValueError("reported actual does not match recomputed value")
  else:
    actual = _number(raw.get("actual"), "actual")

  if relation == "<":
    passed = actual < limit + tolerance
  elif relation == "<=":
    passed = actual <= limit + tolerance
  elif relation == "==":
    passed = abs(actual - limit) <= tolerance
  elif relation == ">=":
    passed = actual >= limit - tolerance
  else:
    passed = actual > limit - tolerance
  return ConstraintResult(
      identifier,
      passed,
      hard,
      relation,
      actual,
      limit,
      independently_recomputed,
  )


def evaluate_constraint_register(payload: Any) -> tuple[tuple[ConstraintResult, ...], int]:
  """Evaluates a register and counts malformed entries instead of raising."""
  if not isinstance(payload, Mapping):
    return (), 1
  raw_items = payload.get("constraints", [])
  if not isinstance(raw_items, list):
    return (), 1
  results: list[ConstraintResult] = []
  malformed = 0
  for raw in raw_items:
    if not isinstance(raw, Mapping):
      malformed += 1
      continue
    try:
      results.append(evaluate_constraint(raw))
    except (ArithmeticError, TypeError, ValueError):
      malformed += 1
  return tuple(results), malformed


def _safe_relative_path(value: Any) -> str:
  path = _identifier(value, "path").replace("\\", "/")
  parsed = PurePosixPath(path)
  if parsed.is_absolute() or ".." in parsed.parts or "." in parsed.parts:
    raise ValueError("path must be a normalized relative path")
  if "\x00" in path:
    raise ValueError("path must not contain a NUL byte")
  return parsed.as_posix()


def _expected_suffix(raw: Mapping[str, Any]) -> str | None:
  value = raw.get("format")
  if value is None:
    return None
  suffix = _identifier(value, "format").lower()
  return suffix if suffix.startswith(".") else f".{suffix}"


def validate_deliverable_declaration(raw: Mapping[str, Any]) -> tuple[str, bool, int, str | None]:
  """Validates and normalizes one deliverable declaration."""
  path = _safe_relative_path(raw.get("path"))
  required = raw.get("required", True)
  if not isinstance(required, bool):
    raise ValueError("required must be boolean")
  minimum = raw.get("min_bytes", 1)
  if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
    raise ValueError("min_bytes must be a non-negative integer")
  return path, required, minimum, _expected_suffix(raw)


def evaluate_deliverables(
    payload: Any,
    present: Mapping[str, tuple[bool, int]],
) -> tuple[tuple[DeliverableResult, ...], int]:
  """Checks declared paths against ``present[path] = (exists, size)``."""
  if not isinstance(payload, Mapping):
    return (), 1
  raw_items = payload.get("files", [])
  if not isinstance(raw_items, list):
    return (), 1
  results: list[DeliverableResult] = []
  malformed = 0
  seen: set[str] = set()
  for raw in raw_items:
    if not isinstance(raw, Mapping):
      malformed += 1
      continue
    try:
      path, required, minimum, suffix = validate_deliverable_declaration(raw)
    except (TypeError, ValueError):
      malformed += 1
      continue
    if path in seen:
      malformed += 1
      continue
    seen.add(path)
    exists, size = present.get(path, (False, 0))
    present_and_nonempty = exists and size >= minimum
    suffix_ok = suffix is None or path.lower().endswith(suffix)
    results.append(
        DeliverableResult(
            path=path,
            required=required,
            present=present_and_nonempty,
            size=max(0, int(size)),
            suffix_ok=suffix_ok,
        )
    )
  return tuple(results), malformed


def validate_authority_record(raw: Mapping[str, Any]) -> AuthorityRecord:
  """Validates one source/revision record."""
  group = _identifier(raw.get("group"), "group")
  path = _safe_relative_path(raw.get("path"))
  revision = _identifier(raw.get("revision"), "revision")
  status = raw.get("status", "current")
  if status not in _AUTHORITY_STATUSES:
    raise ValueError("status is not a supported authority status")
  return AuthorityRecord(group, path, revision, status)


def authority_paths(payload: Any) -> list[str]:
  """Returns validated source paths, without trusting malformed entries."""
  if not isinstance(payload, Mapping) or not isinstance(payload.get("records"), list):
    return []
  paths: list[str] = []
  for raw in payload["records"]:
    if not isinstance(raw, Mapping):
      continue
    try:
      record = validate_authority_record(raw)
    except (TypeError, ValueError):
      continue
    if record.path not in paths:
      paths.append(record.path)
  return paths[:64]


def evaluate_authority_register(
    payload: Any,
    present: Mapping[str, bool] | None = None,
) -> AuthorityReport:
  """Checks source paths and detects competing current revisions."""
  if not isinstance(payload, Mapping):
    return AuthorityReport(malformed=1)
  raw_items = payload.get("records", [])
  if not isinstance(raw_items, list):
    return AuthorityReport(malformed=1)
  unresolved = payload.get("unresolved", [])
  malformed = 0
  if not isinstance(unresolved, list):
    malformed += 1
    unresolved = []
  if not raw_items:
    malformed += 1
  records: list[AuthorityRecord] = []
  seen_paths: set[str] = set()
  current_by_group: dict[str, int] = {}
  for raw in raw_items:
    if not isinstance(raw, Mapping):
      malformed += 1
      continue
    try:
      record = validate_authority_record(raw)
    except (TypeError, ValueError):
      malformed += 1
      continue
    if record.path in seen_paths:
      malformed += 1
      continue
    seen_paths.add(record.path)
    records.append(record)
    if record.status == "current":
      current_by_group[record.group] = current_by_group.get(record.group, 0) + 1
  conflicts = tuple(
      group for group, count in sorted(current_by_group.items()) if count > 1
  )
  represented_groups = {record.group for record in records}
  current_revision_groups = {
      record.group
      for record in records
      if record.status in {"current", "reference", "not_used"}
  }
  missing_current = tuple(sorted(represented_groups - current_revision_groups))
  missing = 0
  if present is not None:
    missing = sum(1 for record in records if not present.get(record.path, False))
  return AuthorityReport(
      records=tuple(records),
      conflict_groups=conflicts,
      missing_current_groups=missing_current,
      missing_records=missing,
      unresolved=len(unresolved),
      malformed=malformed,
  )


def build_report(
    constraint_payload: Any = None,
    deliverable_payload: Any = None,
    present: Mapping[str, tuple[bool, int]] | None = None,
    authority_payload: Any = None,
    authority_present: Mapping[str, bool] | None = None,
) -> GateReport:
  """Builds a report from optional task-local JSON payloads."""
  constraints, malformed_constraints = evaluate_constraint_register(
      constraint_payload
  ) if constraint_payload is not None else ((), 0)
  deliverables, malformed_deliverables = evaluate_deliverables(
      deliverable_payload, present or {}
  ) if deliverable_payload is not None else ((), 0)
  authority = (
      evaluate_authority_register(authority_payload, authority_present)
      if authority_payload is not None
      else None
  )
  return GateReport(
      constraints=constraints,
      deliverables=deliverables,
      authority=authority,
      malformed_constraints=malformed_constraints,
      malformed_deliverables=malformed_deliverables,
  )


def json_object(data: bytes) -> Any:
  """Decodes UTF-8 JSON without exposing the payload in an exception."""
  import json

  try:
    return json.loads(data.decode("utf-8"))
  except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
    return None
