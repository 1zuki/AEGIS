import base64
import json
from pathlib import Path
import unittest

import aegis_codex_mcp


ROOT = Path(__file__).resolve().parents[1]


class CodexMcpTests(unittest.TestCase):
  def make_tools(self):
    return aegis_codex_mcp.TaskTools(
        "http://127.0.0.1:4321/session-token",
        ROOT / "engineering_calcs.py",
    )

  def test_initialize_and_catalog(self):
    tools = self.make_tools()
    initialized = aegis_codex_mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        },
        tools,
    )
    self.assertEqual(initialized["result"]["protocolVersion"], "2024-11-05")
    catalog = aegis_codex_mcp.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, tools
    )
    self.assertEqual(
        {tool["name"] for tool in catalog["result"]["tools"]},
        {
            "read_file",
            "write_file",
            "exec",
            "pipe_hydraulics",
            "beam_elastic_screen",
        },
    )

  def test_read_file_returns_text_or_binary(self):
    tools = self.make_tools()
    tools._relay = lambda *_args, **_kwargs: {
        "content_b64": base64.b64encode(b"task notes").decode(),
        "truncated": False,
    }
    self.assertEqual(
        tools.call("read_file", {"path": "inputs/notes.txt"}),
        {"path": "inputs/notes.txt", "encoding": "utf-8", "content": "task notes"},
    )
    tools._relay = lambda *_args, **_kwargs: {
        "content_b64": base64.b64encode(b"\x00\xff").decode(),
        "truncated": False,
    }
    binary = tools.call("read_file", {"path": "inputs/model.bin"})
    self.assertEqual(binary["encoding"], "base64")
    self.assertEqual(base64.b64decode(binary["content_b64"]), b"\x00\xff")

  def test_image_read_returns_mcp_image_content(self):
    tools = self.make_tools()
    image = b"\x89PNG\r\n\x1a\n"
    tools._relay = lambda *_args, **_kwargs: {
        "content_b64": base64.b64encode(image).decode(),
        "truncated": False,
    }
    result = aegis_codex_mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": "image-call",
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "inputs/plan.png"}},
        },
        tools,
    )
    content = result["result"]["content"]
    self.assertEqual(content[0]["type"], "image")
    self.assertEqual(content[0]["mimeType"], "image/png")
    self.assertEqual(base64.b64decode(content[0]["data"]), image)
    metadata = json.loads(content[1]["text"])
    self.assertEqual(metadata["path"], "inputs/plan.png")
    self.assertEqual(metadata["size_bytes"], len(image))

  def test_read_file_rejects_truncation(self):
    tools = self.make_tools()
    tools._relay = lambda *_args, **_kwargs: {"truncated": True}
    with self.assertRaisesRegex(ValueError, "10 MB"):
      tools.call("read_file", {"path": "large.dat"})

  def test_write_file_encodes_content(self):
    tools = self.make_tools()
    calls = []

    def relay(route, payload, **_kwargs):
      calls.append((route, payload))
      return {"size": len(base64.b64decode(payload["content_b64"]))}

    tools._relay = relay
    result = tools.call(
        "write_file", {"path": "outputs/report.txt", "content": "done"}
    )
    self.assertEqual(result["size"], 4)
    self.assertEqual(calls[0][0], "write_file")
    self.assertEqual(base64.b64decode(calls[0][1]["content_b64"]), b"done")

  def test_calculator_tool_returns_deterministic_json(self):
    tools = self.make_tools()
    response = aegis_codex_mcp.handle(
        {
            "jsonrpc": "2.0",
            "id": "tool-call",
            "method": "tools/call",
            "params": {
                "name": "pipe_hydraulics",
                "arguments": {
                    "flow_m3_s": 0.01,
                    "diameter_m": 0.1,
                    "length_m": 100,
                    "roughness_m": 0.000045,
                    "minor_loss_k": 2,
                },
            },
        },
        tools,
    )
    self.assertFalse(response["result"]["isError"])
    result = json.loads(response["result"]["content"][0]["text"])
    self.assertGreater(result["total_head_loss_m"], 0)
    self.assertIn("m", result["metric_units"]["total_head_loss_m"])

  def test_invalid_relay_host_is_rejected(self):
    with self.assertRaisesRegex(ValueError, "loopback"):
      aegis_codex_mcp.TaskTools(
          "https://remote.example/session-token",
          ROOT / "engineering_calcs.py",
      )

  def test_unknown_tool_uses_json_rpc_method_error(self):
    result = aegis_codex_mcp.handle(
        {"jsonrpc": "2.0", "id": 7, "method": "not/a/method"},
        self.make_tools(),
    )
    self.assertEqual(result["error"]["code"], -32601)


if __name__ == "__main__":
  unittest.main()
