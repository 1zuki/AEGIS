# AEGIS

AEGIS is a WebSocket agent harness for Kaggle's Physical-Engineering's Last
Exam. It starts from the official Pi + Claude Opus 5.5 entry and keeps the
competition protocol bridge and task MCP support. The task-level prompt adds a
short evidence-first workflow for inspecting engineering records, checking
hard safety constraints, and verifying exact deliverables.

Upstream starter: [physical-engineerings-last-exam/submission-template](https://github.com/physical-engineerings-last-exam/submission-template).
The copied harness files retain their Apache 2.0 headers; see
[`LICENSE-APACHE`](LICENSE-APACHE). The repository's original [`LICENSE`](LICENSE)
continues to apply to original AEGIS files.

## Layout

- `serve.py` starts the PLE WebSocket server.
- `harness.py` configures Pi and runs one agent session per task.
- `engineering_calcs.py` provides the pipe and beam screening calculations.
- `aegis_gates.py` independently reruns registered calculations and checks
  deliverables and source/revision bookkeeping after the agent exits.
- `task-tools.ts` routes Pi's file and shell tools to the task container.
- `taskbridge.py` implements the PLE protocol and MCP relay.
- `submission.yaml` selects the Google-sponsored Claude Opus 5.5 endpoint.

## Validate and evaluate

The PLE kit is maintained in the [benchmark repository](https://github.com/google-research/ple), separate from this entry. From a sibling checkout:

```bash
git clone https://github.com/google-research/ple.git ../ple-benchmark
cd ../ple-benchmark
uv run python -m kit validate ../AEGIS
uv run python -m kit check ../AEGIS
uv run python eval/run.py ../AEGIS
uv run python -m kit artifacts ../AEGIS
```

Local build and evaluation require Docker and the kit's Google model proxy
settings. Keep `MODEL_PROXY_URL` and `MODEL_PROXY_KEY` outside this repository.
This working tree does not yet contain `artifacts/`; those must be generated
from the current AEGIS harness before it is submitted.

After installing the declared dependencies, run the protocol smoke test on a
synthetic pipe task with:

```bash
.venv/bin/python tests/run_local_e2e.py
```

By default it starts the actual `serve.py` with GPT-6 Luna through the signed-in
Codex CLI backend. The synthetic evaluator only allows a small read-only shell
command set and confines file operations to a temporary task directory.

## Local model smoke tests

The submitted entry still uses the Google-sponsored model in `submission.yaml`.
For local wiring work, the default `AEGIS_LOCAL_BACKEND=codex` uses GPT-6 Luna
through the signed-in Codex CLI. This uses the existing Codex sign-in and does
not need a separate OpenAI API key. Run the synthetic task with the command
above.

Set `AEGIS_LOCAL_BACKEND=pi` to run Pi against an OpenAI-compatible endpoint
such as Ollama. Start Ollama and make sure `qwen3:4b` is available first.

```bash
export AEGIS_LOCAL_MODE=1
export AEGIS_LOCAL_BACKEND=pi
export AEGIS_LOCAL_MODEL=qwen3:4b
export AEGIS_LOCAL_BASE_URL=http://127.0.0.1:11434/v1
export AEGIS_LOCAL_API_KEY=ollama
```

For Pi, `AEGIS_LOCAL_MODE=1` selects the explicit local model route. The Codex
backend uses the CLI's isolated MCP configuration and exposes only AEGIS task
tools to the model. Both local routes are for smoke tests; keep local mode off
for competition evaluation.

## Visual inputs and deliverables

PLE scores the task's requested engineering files, so AEGIS does not add a
separate universal visualization score. In the official Pi route and the local
Codex bridge, supported raster inputs (`png`, `jpg`, `jpeg`, `gif`, and `webp`)
are passed to the model as image content. The model can create the requested
drawings, models, spreadsheets, and reports through the task tools and MCP
servers; the competition's task-specific checks or rubric grade those files.

The harness runs an independent cold-read pass by default after the main agent.
It uses the same selected model, reopens the saved files, and can repair
concrete errors before deterministic postflight. Set `AEGIS_COLD_READ=0` to
disable that extra pass during a local smoke test.

## Optional postflight manifests

The agent may write these task-local files before it finishes. They are optional;
the harness ignores them when they are absent.

`.aegis/authority_register.json`:

```json
{
  "records": [
    {
      "group": "structural_set",
      "path": "inputs/S-101.pdf",
      "revision": "C",
      "status": "current"
    },
    {
      "group": "structural_set",
      "path": "inputs/S-101-rev-B.pdf",
      "revision": "B",
      "status": "superseded"
    }
  ],
  "unresolved": []
}
```

`.aegis/constraint_register.json`:

```json
{
  "constraints": [
    {
      "id": "head_loss_limit",
      "recompute": {
        "tool": "pipe_hydraulics",
        "parameters": {
          "flow_m3_s": 0.01,
          "diameter_m": 0.1,
          "length_m": 100.0,
          "roughness_m": 0.000045
        },
        "metric": "total_head_loss_m"
      },
      "limit": 15.0,
      "limit_unit": "m",
      "relation": "<=",
      "hard": true,
      "source": "inputs/hydraulic_criteria.txt"
    }
  ]
}
```

`.aegis/deliverables.json`:

```json
{
  "files": [
    {"path": "outputs/pump_schedule.xlsx", "required": true, "format": "xlsx"},
    {"path": "outputs/selection_memo.docx", "required": true, "format": "docx"}
  ]
}
```

After Pi exits, the harness checks supported calculations by rerunning their
formulas, compares them with declared limits in matching units, and checks safe
relative paths, file existence, minimum size, optional filename suffixes,
source existence, and revision conflicts. A manually reported actual value
without a supported recomputation is marked unverified. The summary reports
only counts, so task contents are not copied into the harness log.

Constraint entries can include a `recompute` object with a supported tool,
parameters, and metric. The harness reruns that calculation and requires a
matching `limit_unit`. A manually reported `actual` without a supported
recomputation is marked unverified and cannot produce a clear gate. The pipe
tool uses Darcy-Weisbach/Colebrook-White; the beam tool is a linear-elastic
simply supported uniform-load screen. Neither replaces governing-code checks
or professional review.
