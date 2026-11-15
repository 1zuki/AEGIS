# AEGIS Contributor Instructions

AEGIS is an agent harness for Kaggle's Physical-Engineering's Last Exam (PLE)
competition. The evaluator sends one engineering task over a WebSocket, and the
harness works in the task container through the PLE protocol.

## Competition constraints

- Use a Google-sponsored `google-cloud/<model>` entry. The current supported
  models and protocol are defined by the official PLE competition guide.
- The task container has no internet access. The harness may reach only its
  declared model endpoint through the evaluator's proxy.
- Do not enable web search, external APIs, or model-provider hosted tools.
- Do not log or retain task prompts, project files, or hidden-test content.
- Write deliverables to the exact paths and formats in each task. Use the
  supplied MCP tools when they can inspect or verify engineering files.
- Verify calculations, units, file formats, and hard safety/feasibility
  constraints. Cite source documents for important values, state assumptions,
  and distinguish verified results from unresolved uncertainty.

## Artifacts and evaluation

- Generate `artifacts/` only with the official `eval/run.py`; never edit those
  generated files by hand.
- After changing harness files, rerun the public dev set before preparing a
  submission. The artifact manifest must match the harness version that
  produced it.
- Run the PLE kit checks where the required Docker daemon and local model proxy
  are available. Keep proxy credentials outside this repository.
- Follow the user's global instructions: do not commit, push, or submit without
  an explicit request.
