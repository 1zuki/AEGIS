"""Run the actual AEGIS WebSocket harness on a synthetic local task.

This smoke runner uses a strict task filesystem sandbox and permits only a
small set of read-only shell commands. File reads/writes travel through the
same PLE WebSocket messages used by the evaluator. The model endpoint defaults
to the signed-in Codex CLI backend. Use ``--backend pi`` for an
OpenAI-compatible local endpoint such as Ollama.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sys
import tempfile
import time
from typing import Any

import websockets


ROOT = Path(__file__).resolve().parents[1]
DESIGN_BASIS = """Pipe head-loss screening task
Use the pipe_hydraulics specialist tool with SI inputs:
- flow: 0.01 m^3/s
- inside diameter: 0.10 m
- straight length: 100 m
- absolute roughness: 0.000045 m
- water kinematic viscosity: 0.000001004 m^2/s
- water density: 998.2 kg/m^3
- sum of minor-loss coefficients: 2
Hard project limit: total head loss <= 15 m.
Deliver outputs/hydraulics.md with inputs, velocity, Reynolds number, total
head loss, pressure drop, assumptions, and the limit result. Call this a
preliminary calculation, not code approval.
"""


def task_path(root: Path, raw: str) -> Path:
  candidate = Path(raw)
  candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
  try:
    candidate.relative_to(root.resolve())
  except ValueError:
    raise ValueError("path is outside the synthetic task directory") from None
  return candidate


def _stat_command(command: str, root: Path) -> dict[str, Any] | None:
  match = re.fullmatch(
      r"if test -f(?: --)? (.+); then stat -c %s(?: --)? (.+); else exit 1; fi",
      command.strip(),
  )
  if not match:
    return None
  first = shlex.split(match.group(1))
  second = shlex.split(match.group(2))
  if len(first) != 1 or first != second:
    return {"exit_code": 2, "output": "invalid stat request\n"}
  try:
    path = task_path(root, first[0])
  except ValueError:
    return {"exit_code": 2, "output": "path outside task directory\n"}
  if not path.is_file():
    return {"exit_code": 1, "output": ""}
  return {"exit_code": 0, "output": f"{path.stat().st_size}\n"}


def safe_exec(command: str, root: Path) -> dict[str, Any]:
  stat_result = _stat_command(command, root)
  if stat_result is not None:
    return stat_result
  try:
    args = shlex.split(command)
  except ValueError:
    return {"exit_code": 2, "output": "invalid command syntax\n"}
  if not args or any(token in {";", "&&", "||", "|", ">", "<", "&"} for token in args):
    return {"exit_code": 126, "output": "command is not allowed in local smoke mode\n"}

  if args[0] == "pwd" and len(args) == 1:
    return {"exit_code": 0, "output": f"{root}\n"}

  if args[0] == "ls":
    operands = [arg for arg in args[1:] if not arg.startswith("-")]
    targets = operands or ["."]
    try:
      lines: list[str] = []
      for operand in targets:
        path = task_path(root, operand)
        if path.is_dir():
          lines.extend(sorted(item.name + ("/" if item.is_dir() else "") for item in path.iterdir()))
        elif path.exists():
          lines.append(path.name)
        else:
          return {"exit_code": 2, "output": f"missing path: {operand}\n"}
      return {"exit_code": 0, "output": "\n".join(lines) + "\n"}
    except (OSError, ValueError):
      return {"exit_code": 2, "output": "could not list task path\n"}

  if args[0] == "cat" and len(args) >= 2:
    try:
      chunks = [task_path(root, operand).read_text(encoding="utf-8") for operand in args[1:]]
      output = "".join(chunks)
      if len(output.encode("utf-8")) > 100_000:
        return {"exit_code": 1, "output": "file output exceeds local smoke limit\n"}
      return {"exit_code": 0, "output": output}
    except (OSError, UnicodeError, ValueError):
      return {"exit_code": 1, "output": "could not read task file\n"}

  if args[0] == "test" and len(args) == 4 and args[1:3] == ["-f", "--"]:
    try:
      return {"exit_code": 0 if task_path(root, args[3]).is_file() else 1, "output": ""}
    except ValueError:
      return {"exit_code": 1, "output": ""}

  return {"exit_code": 126, "output": "command is not allowed in local smoke mode\n"}


def task_file(root: Path, raw: str) -> Path:
  return task_path(root, raw)


async def respond(ws: Any, root: Path, message: dict[str, Any]) -> dict[str, Any]:
  kind, rid = message.get("type"), message.get("id")
  if kind == "exec":
    try:
      result = safe_exec(message.get("command", ""), root)
    except Exception:
      result = {"exit_code": 1, "output": "local executor error\n"}
    return {"type": "result", "id": rid, **result, "truncated": False, "timed_out": False}

  if kind == "read_file":
    try:
      path = task_file(root, message.get("path", ""))
      if not path.is_file():
        raise OSError
      data = path.read_bytes()
      if len(data) > 10_000_000:
        return {"type": "result", "id": rid, "ok": False, "error": "file too large"}
      return {
          "type": "result",
          "id": rid,
          "ok": True,
          "content_b64": base64.b64encode(data).decode("ascii"),
          "size": len(data),
          "truncated": False,
      }
    except (OSError, ValueError):
      return {"type": "result", "id": rid, "ok": False, "error": "file unavailable"}

  if kind == "write_file":
    try:
      path = task_file(root, message.get("path", ""))
      data = base64.b64decode(message.get("content_b64", ""), validate=True)
      if len(data) > 10_000_000:
        raise ValueError
      path.parent.mkdir(parents=True, exist_ok=True)
      mode = "ab" if message.get("append") else "wb"
      with path.open(mode) as stream:
        stream.write(data)
      return {"type": "result", "id": rid, "ok": True, "size": path.stat().st_size}
    except (OSError, ValueError):
      return {"type": "result", "id": rid, "ok": False, "error": "write rejected"}

  return {"type": "result", "id": rid, "ok": False, "error": "unsupported request"}


def free_port() -> int:
  with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    return int(sock.getsockname()[1])


async def wait_for_server(process: asyncio.subprocess.Process, port: int) -> None:
  deadline = time.monotonic() + 30
  while time.monotonic() < deadline:
    if process.returncode is not None:
      raise RuntimeError("serve.py exited before accepting connections")
    try:
      reader, writer = await asyncio.open_connection("127.0.0.1", port)
      writer.close()
      await writer.wait_closed()
      return
    except OSError:
      await asyncio.sleep(0.1)
  raise TimeoutError("serve.py did not open its WebSocket port")


async def run(args: argparse.Namespace) -> tuple[str, list[str], Path]:
  with tempfile.TemporaryDirectory(prefix="aegis-ple-e2e-") as temp:
    root = Path(temp).resolve()
    (root / "inputs").mkdir()
    (root / "inputs/design_basis.txt").write_text(DESIGN_BASIS, encoding="utf-8")
    port = free_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "PORT": str(port),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "AEGIS_LOCAL_MODE": "1",
        "AEGIS_LOCAL_BACKEND": args.backend,
        "AEGIS_LOCAL_MODEL": args.model,
        "AEGIS_LOCAL_BASE_URL": args.base_url,
        "AEGIS_LOCAL_API_KEY": args.api_key,
        "AEGIS_LOCAL_THINKING": args.thinking,
        "AEGIS_COLD_READ": "1" if args.cold_read else "0",
    }
    if os.environ.get("CODEX_HOME"):
      env["CODEX_HOME"] = os.environ["CODEX_HOME"]
    process = await asyncio.create_subprocess_exec(
        str(args.python),
        str(ROOT / "serve.py"),
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    tool_logs: list[str] = []
    final_message = ""
    try:
      await wait_for_server(process, port)
      task = {
          "type": "task",
          "protocol": 1,
          "session": "aegis-local-smoke",
          "instruction": (
              "Complete the engineering task in inputs/design_basis.txt. "
              "Use the pipe_hydraulics specialist tool. Create the exact "
              "deliverable and all three .aegis manifests described by the "
              "harness instructions. Do not create other files."
          ),
          "workdir": str(root),
          "limits": {
              "agent_timeout_sec": args.timeout,
              "exec_timeout_sec": 60,
              "max_output_bytes": 100_000,
              "max_write_bytes": 10_000_000,
          },
          "mcp_servers": [],
      }
      async with websockets.connect(
          f"ws://127.0.0.1:{port}",
          ping_interval=None,
          open_timeout=10,
          close_timeout=5,
          max_size=16 * 1024 * 1024,
      ) as ws:
        await ws.send(json.dumps(task))
        deadline = time.monotonic() + args.timeout + 20
        while time.monotonic() < deadline:
          raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
          message = json.loads(raw)
          kind = message.get("type")
          if kind in {"exec", "read_file", "write_file", "http"}:
            await ws.send(json.dumps(await respond(ws, root, message)))
          elif kind == "log":
            text = str(message.get("text", ""))
            if text.startswith((
                "pi: tool ",
                "pi-cold-read: tool ",
                "codex: tool ",
                "codex-cold-read: tool ",
            )):
              tool_logs.append(text)
          elif kind == "done":
            final_message = str(message.get("final_message", ""))
            break
          elif kind == "error":
            raise RuntimeError(f"harness returned error: {message.get('message', 'unknown')}")
        else:
          raise TimeoutError("harness did not finish before the local evaluator deadline")

      report_path = root / "outputs/hydraulics.md"
      if not report_path.is_file():
        raise AssertionError("required hydraulics report was not created")
      report_text = report_path.read_text(encoding="utf-8")
      if not report_text.strip() or "head loss" not in report_text.lower():
        raise AssertionError("hydraulics report is empty or misses the required result")
      if not any("pipe_hydraulics" in item for item in tool_logs):
        raise AssertionError(
            "the agent did not call the pipe_hydraulics specialist; model "
            f"summary: {final_message[:1000]!r}; tool calls: {tool_logs!r}"
        )
      for relative in (
          ".aegis/authority_register.json",
          ".aegis/constraint_register.json",
          ".aegis/deliverables.json",
      ):
        if not (root / relative).is_file():
          raise AssertionError(f"required postflight manifest was not created: {relative}")
      if "AEGIS postflight: CLEAR" not in final_message:
        raise AssertionError(
            "deterministic postflight did not report CLEAR; model summary: "
            f"{final_message[:1600]!r}; tool calls: {tool_logs!r}"
        )
      artifact_copy = ROOT / "runs/local-e2e-hydraulics.md"
      artifact_copy.parent.mkdir(parents=True, exist_ok=True)
      artifact_copy.write_text(report_text, encoding="utf-8")
      return final_message, tool_logs, artifact_copy
    finally:
      if process.returncode is None:
        process.terminate()
        try:
          await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
          process.kill()
          await process.wait()


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--python", type=Path, default=ROOT / ".venv/bin/python")
  parser.add_argument("--backend", choices=("codex", "pi"), default="codex")
  parser.add_argument("--model", default="gpt-6-luna")
  parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
  parser.add_argument("--api-key", default="ollama")
  parser.add_argument("--thinking", default="low")
  parser.add_argument("--timeout", type=int, default=600)
  parser.add_argument("--cold-read", action=argparse.BooleanOptionalAction, default=True)
  args = parser.parse_args()
  final, tools, artifact = asyncio.run(run(args))
  print(f"task completed; tool events logged: {len(tools)}")
  print(final)
  print(f"saved local smoke deliverable: {artifact}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
