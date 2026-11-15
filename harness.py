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

"""PLE engineering agent harness.

The competition route runs one headless Pi session per task. Pi runs in the
serve container while the task's files live in the task container.
`task-tools.ts` replaces Pi's built-in `read`, `write`, `edit`, and `bash` tools
with versions that operate on the task container through `taskbridge.Relay`,
and the task's MCP servers are proxied through the same relay. Pi's agent loop
handles model calls, tool execution, retries, and conversation compaction.
Explicit local mode can instead call the signed-in Codex CLI with an isolated
MCP configuration.

The model is configured from the environment variables set by the evaluator in
`models.json` for each task. Claude and Gemini models use their native APIs
under Pi's built-in provider names (`anthropic` and `google`) so Pi's catalog
supplies their limits, thinking support, and image input capabilities. Any other
model uses the OpenAI-compatible `MODEL_BASE_URL` and `MODEL_API_KEY` under a
custom `model` provider with `UNKNOWN_MODEL` limits.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import aegis_gates
import taskbridge

HERE = pathlib.Path(__file__).resolve().parent
# npm ci, from package-lock.json
PI = shutil.which("pi") or str(HERE / "node_modules" / ".bin" / "pi")
# pi's --thinking level: off, minimal, low, medium, high, xhigh or max
THINKING = "max"
# stop pi this long before the deadline (less on short tasks) so its answer
# arrives
STOP_BEFORE_SEC = 90
CATALOG_TIMEOUT_SEC = 30
# Pi settings: retain the official starter's long idle timeout and retry
# budget for long engineering tasks and model/tool calls.
SETTINGS = {
    "defaultProjectTrust": "never",
    "quietStartup": True,
    "cacheWarming": "streaming",
    "retry": {"maxRetries": 8, "baseDelayMs": 2000, "maxAgentDelayMs": 60000},
    "httpIdleTimeoutMs": 3_600_000,
}
LOCAL_MODE_ENV = "AEGIS_LOCAL_MODE"
LOCAL_MODEL_ENV = "AEGIS_LOCAL_MODEL"
LOCAL_BACKEND_ENV = "AEGIS_LOCAL_BACKEND"
COLD_READ_ENV = "AEGIS_COLD_READ"
COLD_READ_RESERVE_SEC = 180
MAX_POSTFLIGHT_PATHS = 64
# Request features the Google Cloud route for Claude refuses. pi leaves them
# out, and the Anthropic API works without them too.
COMPAT = {
    "supportsStrictTools": False,
    "supportsMidConvoEffort": False,
    "supportsMidConvoSystemMessages": False,
    "supportsMidConvoToolChanges": False,
}
# A model pi's catalog does not know gets these limits; tune them for yours.
UNKNOWN_MODEL = {"input": ["text"], "contextWindow": 128000, "maxTokens": 16384}
NOTE = (
    "You are an engineering delivery agent working in a Linux task container"
    " with no internet access. Your tools act in the task container, whose"
    " working directory is {workdir}. Follow this workflow:\n"
    "1. Read the full task. Identify every required deliverable, exact path and"
    " format, acceptance check, hard safety or feasibility constraint, and"
    " time limit. Keep this checklist brief.\n"
    "2. Inspect the project files and relevant revisions before calculating."
    " Use the provided tools, including task MCP tools, when they can read or"
    " verify drawings, models, spreadsheets, or documents. If the inputs are"
    " raster drawing sheets (PNG, JPG, GIF, or WEBP), call read_file on each"
    " relevant sheet and inspect the returned image content directly before"
    " modeling. Transcribe the dimensions, labels, elevations, revisions, and"
    " other governing notes into your plan; do not rely on filenames alone.\n"
    "3. Resolve conflicting inputs from explicit task instructions and current"
    " approved project records. Cite the source for important values. State"
    " material uncertainty and assumptions instead of inventing missing facts.\n"
    "4. Work in small steps. Use code or engineering software for calculations"
    " that need precision. Show units and key assumptions. Check every hard"
    " safety and feasibility limit before optimizing cost or schedule.\n"
    "5. Before calculating, record the source/revision decision in"
    " .aegis/authority_register.json. Use one record per input file, mark"
    " superseded or reference files, and put unresolved conflicts in the"
    " unresolved list. Select at most one current revision per source group."
    " Do not call a source current unless the task records"
    " support that choice. Use this shape with paths relative to the task"
    " directory: {{\"records\":[{{\"group\":\"...\",\"path\":\"...\","
    "\"revision\":\"...\",\"status\":\"current|superseded|reference|not_used\"}}],"
    "\"unresolved\":[]}}.\n"
    "6. Write a complete first version of each deliverable early at the exact"
    " requested path. Then reopen or parse each file, verify its format and"
    " contents, and independently recheck important numbers and constraints."
    " Use a three-pass loop: source audit, specialist calculation in a small"
    " reproducible script, then a cold-read check of the saved deliverables."
    " Fix gaps before spending time on optional polish. If practical, record"
    " numeric checks in .aegis/constraint_register.json and final paths in"
    " .aegis/deliverables.json so the harness can run a deterministic"
    " postflight check. For a constraint that can be checked by a supplied"
    " calculator, record recompute.tool, recompute.parameters, and"
    " recompute.metric so the harness independently reruns it. The constraint"
    " shape is {{\"constraints\":[{{\"id\":\"...\","
    "\"recompute\":{{\"tool\":\"pipe_hydraulics\","
    "\"parameters\":{{...}},\"metric\":\"total_head_loss_m\"}},"
    "\"limit\":0,\"limit_unit\":\"m\","
    "\"relation\":\"<=|>=|==|<|>\",\"hard\":true,"
    "\"source\":\"...\"}}]}}; a constraint with only a reported actual is"
    " marked unverified. The deliverable"
    " shape is {{\"files\":[{{\"path\":\"...\",\"required\":true,"
    "\"format\":\"md\"}}]}}. When present, format must match the file"
    " extension without a dot (for example, md for .md). Omit format if you"
    " cannot confirm the extension.\n"
    "7. Reserve time to finish cleanly. Reply with a concise summary of the"
    " deliverables, exact paths, key results, and any unresolved limitation."
    " Do not claim a licensed engineer reviewed or approved the work."
)
COLD_READ_NOTE = (
    "You are the independent cold-read verifier for the same engineering task."
    " The first agent has already produced deliverables in {workdir}. Read the"
    " original task and reopen every required output from disk. Check the"
    " authority register, units, hard safety or feasibility limits, file"
    " formats, and required paths. Use small deterministic scripts when a"
    " number needs recomputation. Fix concrete errors you find, update the"
    " optional .aegis manifests, and leave valid files unchanged. Do not"
    " invent missing source data or claim licensed approval. Finish with a"
    " concise verification summary."
)


def local_mode() -> bool:
  """Returns whether an explicit local model route is enabled."""
  return os.environ.get(LOCAL_MODE_ENV, "").lower() in {"1", "true", "yes"}


def local_backend() -> str:
  """Returns the explicitly selected local agent backend."""
  backend = os.environ.get(LOCAL_BACKEND_ENV, "codex").strip().lower()
  if backend not in {"codex", "pi"}:
    raise RuntimeError("AEGIS_LOCAL_BACKEND must be codex or pi")
  return backend


def cold_read_enabled() -> bool:
  """Returns whether the second, independent file-review pass is enabled."""
  return os.environ.get(COLD_READ_ENV, "1").lower() in {"1", "true", "yes"}


def pi_thinking_level() -> str:
  """Returns Pi's thinking level, with an explicit local override."""
  level = os.environ.get("AEGIS_LOCAL_THINKING", "").strip().lower()
  return level or THINKING


def model_name() -> str:
  """Returns the evaluator model, or the explicitly selected local model."""
  if local_mode():
    return os.environ.get(LOCAL_MODEL_ENV, "gpt-6-luna")
  return os.environ["MODEL_NAME"]


def provider() -> tuple[str, dict[str, Any]]:
  """Returns the Pi provider name and its `models.json` entry.

  Returns:
    A tuple of `(provider_name, provider_config)`.
  """
  if local_mode():
    base_url = (
        os.environ.get("AEGIS_LOCAL_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("MODEL_BASE_URL")
        or (
            "https://api.openai.com/v1"
            if os.environ.get("OPENAI_API_KEY")
            else None
        )
    )
    if not base_url:
      raise RuntimeError(
          "AEGIS_LOCAL_MODE=1 requires AEGIS_LOCAL_BASE_URL, "
          "OPENAI_BASE_URL, or MODEL_BASE_URL"
      )
    if os.environ.get("AEGIS_LOCAL_API_KEY"):
      api_key = "$AEGIS_LOCAL_API_KEY"
    elif os.environ.get("OPENAI_API_KEY"):
      api_key = "$OPENAI_API_KEY"
    else:
      api_key = "$MODEL_API_KEY"
    return "model", {
        "baseUrl": base_url,
        "api": "openai-completions",
        "apiKey": api_key,
    }
  if os.environ.get("ANTHROPIC_BASE_URL"):
    return "anthropic", {
        "baseUrl": os.environ["ANTHROPIC_BASE_URL"],
        "api": "anthropic-messages",
        "apiKey": "$ANTHROPIC_API_KEY",
        "compat": COMPAT,
    }
  if os.environ.get("GOOGLE_GEMINI_BASE_URL"):
    return "google", {
        "baseUrl": os.environ["GOOGLE_GEMINI_BASE_URL"] + "/v1beta",
        "api": "google-generative-ai",
        "apiKey": "$GEMINI_API_KEY",
    }
  return "model", {
      "baseUrl": os.environ["MODEL_BASE_URL"],
      "api": "openai-completions",
      "apiKey": "$MODEL_API_KEY",
  }


def in_catalog(name: str, model: str, env: dict[str, str]) -> bool:
  """Checks whether Pi's built-in model catalog recognizes `model`.

  If the probe hangs beyond `CATALOG_TIMEOUT_SEC` or fails, returns `False` so
  the model is configured explicitly with `UNKNOWN_MODEL` limits.

  Args:
    name: Provider name (`anthropic`, `google`, or `model`).
    model: Model identifier string.
    env: Subprocess environment variables for the Pi CLI probe.

  Returns:
    `True` if `(name, model)` appears in `pi --list-models`, else `False`.
  """
  try:
    out = subprocess.run(
        [PI, "--offline", "--list-models", model],
        env=env,
        capture_output=True,
        text=True,
        timeout=CATALOG_TIMEOUT_SEC,
        check=False,
    ).stdout
  except (subprocess.TimeoutExpired, OSError):
    return False
  return any(line.split()[:2] == [name, model] for line in out.splitlines())


def configure(
    agent_dir: pathlib.Path,
    relay: taskbridge.Relay,
    session: taskbridge.Session,
    env: dict[str, str],
) -> tuple[str, str]:
  """Writes Pi's model, settings, and MCP configuration files for a task.

  Args:
    agent_dir: Directory where Pi reads its configuration files.
    relay: Local HTTP relay for the task container and its MCP servers.
    session: Active task session.
    env: Environment dictionary passed to Pi subprocesses.

  Returns:
    A tuple of `(provider_name, model_name)`.
  """
  model = model_name()
  name, config = provider()
  models = agent_dir / "models.json"
  # `pi --list-models` reads the provider here
  models.write_text(json.dumps({"providers": {name: config}}))
  if not in_catalog(name, model, env):
    config["models"] = [{"id": model, "name": model, **UNKNOWN_MODEL}]
    models.write_text(json.dumps({"providers": {name: config}}))
  (agent_dir / "settings.json").write_text(json.dumps(SETTINGS))
  servers = {
      s["name"]: {"url": relay.mcp_url(s["name"]), "timeout": 300}
      for s in session.mcp_servers
  }
  (agent_dir / "mcp.json").write_text(json.dumps({"mcpServers": servers}))
  return name, model


class Run:
  """Tracks JSON stream events from a single Pi subprocess run."""

  def __init__(self, session: taskbridge.Session, model: str):
    self.session, self.model, self.replies = session, model, 0

  async def on_event(self, event: dict[str, Any]) -> None:
    """Processes a parsed JSON event from Pi, updating final message and usage.

    Args:
      event: Decoded JSON event dictionary from Pi's stdout stream.
    """
    kind, message = event.get("type"), event.get("message") or {}
    if kind == "message_end" and message.get("role") == "assistant":
      self.replies += 1
      text = "\n".join(
          b.get("text", "")
          for b in message.get("content") or []
          if b.get("type") == "text"
      )
      if text.strip():
        self.session.final_message = text.strip()
      if message.get("stopReason") == "error":
        await self.session.log("pi: model error")
      # input_tokens counts cached prompt tokens too
      u = message.get("usage") or {}
      await self.session.usage(
          self.model,
          input_tokens=u.get("input", 0)
          + u.get("cacheRead", 0)
          + u.get("cacheWrite", 0),
          output_tokens=u.get("output", 0),
          cache_read_tokens=u.get("cacheRead", 0),
      )
    elif kind == "tool_execution_start":
      await self.session.log(f"pi: tool {event.get('toolName', 'unknown')}")
    elif kind in ("auto_retry_start", "compaction_start"):
      await self.session.log(f"pi: {kind}")

  async def on_line(self, line: bytes) -> None:
    """Parses a raw stdout line from Pi as JSON and forwards it to `on_event`.

    Args:
      line: Raw line bytes emitted by Pi.
    """
    try:
      event = json.loads(line)
    except ValueError:
      return
    if isinstance(event, dict):
      await self.on_event(event)


class CodexRun:
  """Tracks final messages, usage, and MCP tool calls from Codex JSONL."""

  def __init__(self, session: taskbridge.Session, model: str):
    self.session, self.model, self.replies = session, model, 0

  async def on_line(self, line: bytes) -> None:
    try:
      event = json.loads(line)
    except ValueError:
      return
    if not isinstance(event, dict):
      return
    item = event.get("item") or {}
    if not isinstance(item, dict):
      item = {}
    event_type = event.get("type")
    item_type = item.get("type")
    if event_type in {"item.started", "item.completed"} and item_type in {
        "mcp_tool_call",
        "tool_call",
    }:
      name = item.get("tool") or item.get("tool_name") or item.get("name")
      if isinstance(name, str) and name:
        await self.session.log(f"codex: tool {name}")
    if event_type == "item.completed" and item_type in {
        "agent_message",
        "assistant_message",
    }:
      message = item.get("text")
      if isinstance(message, str) and message.strip():
        self.replies += 1
        self.session.final_message = message.strip()
    if event_type == "turn.completed":
      usage = event.get("usage") or event.get("total_token_usage") or {}
      if isinstance(usage, dict):
        await self.session.usage(
            self.model,
            input_tokens=usage.get("input_tokens", usage.get("input", 0)),
            output_tokens=usage.get("output_tokens", usage.get("output", 0)),
            cache_read_tokens=usage.get(
                "cached_input_tokens", usage.get("cache_read_tokens", 0)
            ),
        )


def _codex_environment() -> dict[str, str]:
  """Builds a small environment for Codex without forwarding API secrets."""
  home = str(pathlib.Path.home())
  env = {
      "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
      "HOME": home,
  }
  if os.environ.get("CODEX_HOME"):
    env["CODEX_HOME"] = os.environ["CODEX_HOME"]
  for key in ("TMPDIR", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
    if os.environ.get(key):
      env[key] = os.environ[key]
  return env


def _codex_argv(
    model: str,
    relay: taskbridge.Relay,
    agent_dir: pathlib.Path,
    work: pathlib.Path,
    last_message: pathlib.Path,
    session: taskbridge.Session,
) -> list[str]:
  """Builds an isolated Codex CLI invocation with only AEGIS task tools."""
  codex = shutil.which("codex")
  if not codex:
    raise RuntimeError(
        "the local Codex backend requires the codex CLI and an active sign-in"
    )
  argv = [
      codex,
      "exec",
      "--model",
      model,
      "--ephemeral",
      "--ignore-user-config",
      "--skip-git-repo-check",
      "--sandbox",
      "read-only",
      "--color",
      "never",
      "--json",
      "--output-last-message",
      str(last_message),
      "-C",
      str(work),
  ]
  config = [
      ("model_reasoning_effort", '"low"'),
      ("mcp_optional_startup_grace_ms", "0"),
      ("mcp_servers.aegis.command", json.dumps(sys.executable)),
      (
          "mcp_servers.aegis.args",
          json.dumps(
              [
                  str(HERE / "aegis_codex_mcp.py"),
                  "--calculator-path",
                  str(HERE / "engineering_calcs.py"),
              ]
          ),
      ),
      ("mcp_servers.aegis.cwd", json.dumps(str(HERE))),
      (
          "mcp_servers.aegis.env.TASK_RELAY_URL",
          json.dumps(relay.url),
      ),
      (
          "mcp_servers.aegis.env.NO_PROXY",
          json.dumps("127.0.0.1,localhost"),
      ),
      (
          "mcp_servers.aegis.env.no_proxy",
          json.dumps("127.0.0.1,localhost"),
      ),
      ("mcp_servers.aegis.startup_timeout_sec", "20"),
      ("mcp_servers.aegis.tool_timeout_sec", "300"),
      ("mcp_servers.aegis.required", "true"),
      ("mcp_servers.aegis.default_tools_approval_mode", '"approve"'),
      (
          "mcp_servers.aegis.enabled_tools",
          json.dumps([tool["name"] for tool in aegis_codex_tools()]),
      ),
  ]
  for index, server in enumerate(session.mcp_servers):
    server_name = server.get("name")
    if not isinstance(server_name, str) or not server_name:
      continue
    config.extend(
        [
            (f"mcp_servers.task_mcp_{index}.url", json.dumps(relay.mcp_url(server_name))),
            (f"mcp_servers.task_mcp_{index}.startup_timeout_sec", "20"),
            (f"mcp_servers.task_mcp_{index}.tool_timeout_sec", "300"),
            (f"mcp_servers.task_mcp_{index}.default_tools_approval_mode", '"approve"'),
        ]
    )
  for key, value in config:
    argv.extend(("--config", f"{key}={value}"))
  argv.append("-")
  return argv


def aegis_codex_tools() -> list[dict[str, Any]]:
  """Returns the local MCP tool catalog used to allowlist Codex tools."""
  # Kept in one small import to avoid duplicating its schema definitions.
  import aegis_codex_mcp

  return aegis_codex_mcp.TOOLS


async def _run_codex_pass(
    session: taskbridge.Session,
    relay: taskbridge.Relay,
    agent_dir: pathlib.Path,
    work: pathlib.Path,
    model: str,
    prompt: str,
    stop_before_sec: float,
    log_prefix: str,
) -> tuple[taskbridge.Finished, CodexRun]:
  last_message = agent_dir / "last-message.txt"
  with contextlib.suppress(FileNotFoundError):
    last_message.unlink()
  run = CodexRun(session, model)
  finished = await taskbridge.run_process(
      _codex_argv(model, relay, agent_dir, work, last_message, session),
      session,
      env=_codex_environment(),
      cwd=str(work),
      stdin=prompt.encode("utf-8"),
      on_line=run.on_line,
      stop_before_sec=stop_before_sec,
      log_prefix=log_prefix,
  )
  if last_message.is_file():
    with contextlib.suppress(OSError, UnicodeError):
      message = last_message.read_text(encoding="utf-8").strip()
      if message:
        run.replies = max(run.replies, 1)
        session.final_message = message
  return finished, run


async def _solve_with_codex(
    session: taskbridge.Session,
    relay: taskbridge.Relay,
    agent_dir: pathlib.Path,
    work: pathlib.Path,
) -> str:
  """Runs the local Codex model using taskbridge-backed MCP tools."""
  model = model_name()
  prompt = NOTE.format(workdir=session.workdir) + "\n\nTask:\n" + session.instruction
  done, run = await _run_codex_pass(
      session,
      relay,
      agent_dir,
      work,
      model,
      prompt,
      COLD_READ_RESERVE_SEC if cold_read_enabled() else STOP_BEFORE_SEC,
      "codex: ",
  )
  first_message = session.final_message
  if done.code != 0 and not run.replies:
    raise RuntimeError(
        f"codex exited with {done.code} before the model answered;"
        " stderr details were suppressed"
    )

  left = session.time_left()
  if cold_read_enabled() and (left is None or left > 5) and run.replies:
    verifier_prompt = (
        COLD_READ_NOTE.format(workdir=session.workdir)
        + "\n\nOriginal task:\n"
        + session.instruction
        + "\n\nPerform the independent cold-read pass now."
    )
    verifier_done, verifier = await _run_codex_pass(
        session,
        relay,
        agent_dir,
        work,
        model,
        verifier_prompt,
        STOP_BEFORE_SEC,
        "codex-cold-read: ",
    )
    if verifier.replies:
      verification_message = session.final_message
      if first_message and verification_message and verification_message != first_message:
        session.final_message = (
            f"{first_message}\n\nCold-read verification:\n"
            f"{verification_message}"
        )
      run = verifier
    elif verifier_done.code != 0:
      await session.log("codex-cold-read: verifier did not finish cleanly")

  report = await postflight(session)
  if report is not None:
    summary = report.summary()
    await session.log(summary)
    return f"{session.final_message or '(codex stopped without a final message)'}\n\n{summary}"
  return session.final_message or "(codex stopped without a final message)"


async def solve(session: taskbridge.Session) -> str:
  """Runs Pi on a single task and returns the final response.

  Creates an isolated temporary agent home, configures tools and MCP servers to
  communicate through `taskbridge.Relay`, and feeds the task instruction on
  standard input.

  Args:
    session: Active task session.

  Returns:
    The final message produced by Pi.

  Raises:
    RuntimeError: If Pi exits before the model produces any assistant reply.
  """
  report: aegis_gates.GateReport | None = None
  first_message = ""
  with tempfile.TemporaryDirectory(
      prefix="pi-",
      dir=os.environ.get("HOME") or None,
      ignore_cleanup_errors=True,
  ) as scratch:
    agent_dir, work = (
        pathlib.Path(scratch) / "agent",
        pathlib.Path(scratch) / "work",
    )
    agent_dir.mkdir()
    work.mkdir()
    async with taskbridge.Relay(session) as relay:
      if local_mode() and local_backend() == "codex":
        return await _solve_with_codex(session, relay, agent_dir, work)
      env = relay.env()
      env.update(
          PI_CODING_AGENT_DIR=str(agent_dir),
          PI_OFFLINE="1",
          PI_SKIP_VERSION_CHECK="1",
          PI_TELEMETRY="0",
          NODE_NO_WARNINGS="1",
          TASK_WORKDIR=session.workdir,
          AEGIS_PYTHON=sys.executable,
          AEGIS_CALCULATOR_PATH=str(HERE / "engineering_calcs.py"),
      )
      name, model = await asyncio.to_thread(
          configure, agent_dir, relay, session, env
      )
      base_argv = [
          PI,
          "--print",
          "--mode",
          "json",
          "--no-session",
          "-nc",
          "-ns",
          "-np",
          "-na",
          "-e",
          str(HERE / "task-tools.ts"),
          "--provider",
          name,
          "--model",
          model,
          "--thinking",
          pi_thinking_level(),
      ]
      argv = [
          *base_argv,
          "--append-system-prompt",
          NOTE.format(workdir=session.workdir),
      ]
      run = Run(session, model)
      done = await taskbridge.run_process(
          argv,
          session,
          env=env,
          cwd=str(work),
          on_line=run.on_line,
          stdin=session.instruction.encode(),
          stop_before_sec=(
              COLD_READ_RESERVE_SEC
              if cold_read_enabled()
              else STOP_BEFORE_SEC
          ),
          log_prefix="pi: ",
      )
      first_message = session.final_message
      left = session.time_left()
      if cold_read_enabled() and (left is None or left > 5) and run.replies:
        verifier = Run(session, model)
        await taskbridge.run_process(
            [
                *base_argv,
                "--append-system-prompt",
                COLD_READ_NOTE.format(workdir=session.workdir),
            ],
            session,
            env=env,
            cwd=str(work),
            on_line=verifier.on_line,
            stdin=(
                session.instruction
                + "\n\nPerform the independent cold-read pass now."
            ).encode(),
            stop_before_sec=STOP_BEFORE_SEC,
            log_prefix="pi-cold-read: ",
        )
        if verifier.replies:
          verification_message = session.final_message
          if (
              first_message
              and verification_message
              and verification_message != first_message
          ):
            session.final_message = (
                f"{first_message}\n\nCold-read verification:\n"
                f"{verification_message}"
            )
          run = verifier
      report = await postflight(session)
  if not run.replies:
    raise RuntimeError(
        f"pi exited with {done.code} before the model answered;"
        " stderr details were suppressed"
    )
  final = session.final_message or "(pi stopped without a final message)"
  if report is not None:
    summary = report.summary()
    await session.log(summary)
    final = f"{final}\n\n{summary}"
  return final


async def _optional_json(
    session: taskbridge.Session, path: str
) -> tuple[Any, bool]:
  """Reads a task-local JSON file without logging its contents."""
  result = await session.read_result(path)
  if not result.get("ok"):
    return None, False
  try:
    import base64

    payload = base64.b64decode(result.get("content_b64", ""))
  except (TypeError, ValueError):
    return None, True
  return aegis_gates.json_object(payload), True


async def _file_stat(session: taskbridge.Session, path: str) -> tuple[bool, int]:
  """Checks a task file without transferring its contents to the harness."""
  quoted = shlex.quote(path)
  result = await session.exec(
      f"if test -f {quoted}; then stat -c %s {quoted}; else exit 1; fi",
      timeout_sec=10,
  )
  if result.get("exit_code") != 0:
    return False, 0
  try:
    size = int(str(result.get("output", "")).strip())
  except (TypeError, ValueError):
    return False, 0
  return True, max(0, size)


def _declared_paths(payload: Any) -> list[str]:
  """Extracts safe paths for existence checks without trusting raw paths."""
  if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
    return []
  paths: list[str] = []
  for raw in payload["files"]:
    if not isinstance(raw, dict):
      continue
    try:
      path, _, _, _ = aegis_gates.validate_deliverable_declaration(raw)
    except (TypeError, ValueError):
      continue
    if path not in paths:
      paths.append(path)
  return paths[:MAX_POSTFLIGHT_PATHS]


async def postflight(session: taskbridge.Session) -> aegis_gates.GateReport | None:
  """Runs optional deterministic gates against task-local manifests."""
  constraint_payload, constraint_found = await _optional_json(
      session, aegis_gates.CONSTRAINT_REGISTER
  )
  deliverable_payload, deliverable_found = await _optional_json(
      session, aegis_gates.DELIVERABLE_MANIFEST
  )
  authority_payload, authority_found = await _optional_json(
      session, aegis_gates.AUTHORITY_REGISTER
  )
  if not constraint_found and not deliverable_found and not authority_found:
    return None

  present: dict[str, tuple[bool, int]] = {}
  for path in _declared_paths(deliverable_payload):
    present[path] = await _file_stat(session, path)

  authority_present: dict[str, bool] = {}
  for path in aegis_gates.authority_paths(authority_payload)[
      :MAX_POSTFLIGHT_PATHS
  ]:
    present_on_task = await _file_stat(session, path)
    authority_present[path] = present_on_task[0]

  # A found-but-invalid JSON file is represented as one malformed entry. This
  # keeps the result visible without copying the malformed task content into
  # logs or the model response.
  if constraint_found and constraint_payload is None:
    constraint_payload = {"constraints": [None]}
  if deliverable_found and deliverable_payload is None:
    deliverable_payload = {"files": [None]}
  if authority_found and authority_payload is None:
    authority_payload = {"records": [None]}
  return aegis_gates.build_report(
      constraint_payload if constraint_found else None,
      deliverable_payload if deliverable_found else None,
      present,
      authority_payload if authority_found else None,
      authority_present,
  )
