# GDPVal Benchmark

A framework-agnostic evaluation pipeline for the **GDPVal** benchmark — 220 occupational tasks spanning 44 occupations across 9 sectors. GDPVal measures an agent's ability to produce grounded work artifacts (documents, spreadsheets, code, etc.) that meet professional standards.

This repository contains:

- **Benchmark tasks** — 220 tasks from the [OpenAI GDPVal dataset](https://huggingface.co/datasets/openai/gdpval) (50 bundled, full set downloadable from HuggingFace)
- **Evaluation rubrics** — 44 occupation-specific meta-prompts for LLM-based scoring
- **Agent runner** — CLI-driven agent execution with optional bwrap sandbox isolation
- **Evaluation pipeline** — CLI tool to score agent outputs against rubrics

You can evaluate **any** CLI agent — Claude Code, OpenHands, SWE-agent, custom scripts, or any tool that runs from the command line.

## Quick Start

### 1. Install

```bash
pip install -e .

# Optional: for evaluating .docx/.xlsx artifacts
pip install -e ".[docs]"
```

### 2. Set up evaluation API key

The evaluator uses an OpenAI-compatible LLM (default: `gpt-4o`) to score artifacts:

```bash
cp .env.example .env
# Edit .env with your API key
```

| Env Variable | Purpose |
|---|---|
| `EVALUATION_API_KEY` | Dedicated API key for evaluation |
| `EVALUATION_API_BASE` | Custom endpoint (e.g., Azure OpenAI) |
| `EVALUATION_MODEL` | Override model (default: `gpt-4o`) |
| `OPENAI_API_KEY` | Fallback if `EVALUATION_API_KEY` not set |

### 3. Export tasks

```bash
# Export all 50 bundled tasks to a workspace directory
python -m gdpval_bench export-tasks --output workspace/

# Export a subset
python -m gdpval_bench export-tasks --output workspace/ --max-tasks 5

# Export using a fixed task list
python -m gdpval_bench export-tasks --output workspace/ --task-list gdpval_bench/tasks_50.json
```

This creates one directory per task:

```
workspace/
  manifest.json
  {task_id}/
    task.json          # Task metadata + prompt
    reference_file.pdf # Downloaded reference files (if any)
    ...
```

### 4. Configure your agents

Edit `agent_config.yaml` to register one or more CLI agents. Each top-level key is the agent name:

```yaml
claude:
  command: "claude -p {prompt} --output-format text --max-turns 30"
  output_format: text   # "text" (default) or "json"
  timeout: 1800         # Per-task timeout in seconds (0 = no timeout)
  concurrency: 1        # Reserved for future parallel execution
  use_bwrap: false      # Enable bwrap sandbox isolation (Linux only)
  env: {}               # Extra environment variables for the agent process

openhands:
  command: "python -m my_agent --task-file {task_json} --workspace {workspace}"
  output_format: json   # parses {"type": "result", "text": "..."} from stdout
  timeout: 3600
  concurrency: 1
  env: {}
  use_bwrap: false

custom:
  command: "bash run_agent.sh {prompt_file} {workspace}"
  timeout: 1800
  concurrency: 1
  env: {}
  use_bwrap: false
```

**`output_format`** controls how the agent's stdout is parsed:

- `text` (default) — stdout is used as-is.
- `json` — each line of stdout is parsed as JSON. Lines containing a `"text"` field (e.g. `{"type": "result", "text": "..."}`) have their text extracted and concatenated. This is compatible with OpenHands-style JSON output.

**Available placeholders:**

| Placeholder | Value |
|---|---|
| `{workspace}` | Absolute path to the task workspace directory (shell-escaped) |
| `{prompt}` | Full task prompt text (shell-escaped) |
| `{prompt_file}` | Path to a temp file containing the prompt (for long prompts) |
| `{task_id}` | The task ID string (shell-escaped) |
| `{task_json}` | Absolute path to the task's `task.json` file (shell-escaped) |

### 5. Run your agent

```bash
# Run on all exported tasks (uses the sole agent if only one is defined)
python -m gdpval_bench run --workspace workspace/

# Select a named agent
python -m gdpval_bench run --workspace workspace/ --agent claude

# Run on a single task
python -m gdpval_bench run --workspace workspace/ --agent claude --task-id <task_id>

# Name your run (for organizing logs)
python -m gdpval_bench run --workspace workspace/ --agent claude --run-name claude_v1
```

The runner iterates over each task directory, expands the command template with the task's prompt and paths, and executes the agent as a subprocess. Per-task results are logged to `gdpval_bench/results/<run_name>/run_log.jsonl`.

### 6. Evaluate

```bash
# Evaluate all tasks
python -m gdpval_bench evaluate --workspace workspace/

# Evaluate a single task
python -m gdpval_bench evaluate --workspace workspace/ --task-id <task_id>

# Resume interrupted evaluation
python -m gdpval_bench evaluate --workspace workspace/ --resume

# Name your run
python -m gdpval_bench evaluate --workspace workspace/ --run-name claude_v1
```

Results are saved to `gdpval_bench/results/<run_name>/`:

```
results/<run_name>/
  run_log.jsonl     # Per-task agent execution log (from `run`)
  results.jsonl     # Per-task evaluation results (from `evaluate`)
  summary.json      # Aggregate statistics (from `evaluate`)
```

## Sandbox Isolation (bwrap)

When running on Linux, you can enable [bubblewrap](https://github.com/containers/bubblewrap) sandbox isolation to restrict each agent invocation to its own task workspace:

```yaml
# In agent_config.yaml
use_bwrap: true
```

**Sandbox policy:**

- Host filesystem is mounted **read-only** (`--ro-bind / /`)
- The task workspace directory is mounted **read-write** (`--bind`)
- `/dev` and `/proc` are re-mounted for basic system access
- `TMPDIR` is redirected to the workspace (since `/tmp` is read-only)
- `PIP_TARGET` / `PYTHONPATH` point to `.pip_packages/` in the workspace
- Network is **not** isolated (agents may need HTTP access)
- `--die-with-parent` ensures cleanup if the parent process exits

**Requirements:**

```bash
# Install bubblewrap
sudo apt install bubblewrap
```

This prevents agents from modifying files outside their designated workspace, providing isolation between tasks and protecting the host system.

## How Evaluation Works

### Scoring

Each task is scored on a **0-10 scale** using an occupation-specific rubric. The rubrics evaluate four dimensions:

| Dimension | Typical Weight | What It Checks |
|---|---|---|
| **Completeness** | 40% | All required deliverables present |
| **Correctness** | 30% | Accurate content, correct calculations |
| **Quality** | 20% | Professional formatting and structure |
| **Domain Standards** | 10% | Occupation-specific best practices |

Scores are normalized to 0.0-1.0 for the final result.

### Acceptance Threshold

A score >= **0.6** (6/10) counts as **accepted**. Below this threshold the submission is **rejected**. This mirrors the ClawWork payment cliff used in the original GDPVal benchmark.

### Artifact Discovery

The evaluator automatically finds agent-created files in each task directory. It:

- Scans for common artifact types: `.pdf`, `.docx`, `.xlsx`, `.pptx`, `.txt`, `.csv`, `.json`, `.md`, `.py`, `.js`, `.html`, `.css`, `.png`, `.jpg`, etc.
- Excludes reference files (input files) from evaluation
- Excludes build/environment directories (`.pip_packages/`, `node_modules/`, `__pycache__/`, etc.)
- Skips empty files

### Rubrics

The 44 occupation-specific rubrics are in `gdpval_bench/meta_prompts/`. Each is a detailed JSON prompt that instructs the evaluator LLM how to assess artifacts for that occupation.

## CLI Reference

### `export-tasks` — Export tasks to a workspace

```
python -m gdpval_bench export-tasks [OPTIONS]

Options:
  --output, -o PATH      Output directory (default: workspace/)
  --task-list PATH        Use a fixed task list JSON file
  --max-tasks N           Limit number of tasks
  --per-occupation N      Stratified sampling: N tasks per occupation
  --gdpval-path PATH     Path to GDPVal parquet or directory
  --sectors SECTOR [...]  Filter by sector(s)
  --occupations OCC [...] Filter by occupation(s)
  --no-prefetch           Skip reference file download
```

### `run` — Run a CLI agent on each task

```
python -m gdpval_bench run [OPTIONS]

Options:
  --workspace, -w PATH   Workspace directory (default: workspace/)
  --agent NAME           Name of the agent to run (from agent_config.yaml)
  --agent-config PATH    Path to agent_config.yaml (default: auto-detect)
  --task-id ID           Run agent on a single task
  --run-name NAME        Name for this run (used for log directory)
```

Agents are configured in `agent_config.yaml` as named entries. If the config contains a single agent, `--agent` can be omitted. The config file is searched in this order:

1. Path given by `--agent-config`
2. `agent_config.yaml` in the current working directory
3. `agent_config.yaml` in the repository root

### `evaluate` — Score agent outputs

```
python -m gdpval_bench evaluate [OPTIONS]

Options:
  --workspace, -w PATH   Workspace directory (required)
  --task-id ID           Evaluate a single task
  --run-name NAME        Name for results directory
  --resume               Skip already-evaluated tasks
  --task-list PATH       Use a fixed task list JSON file
  --max-tasks N          Limit number of tasks
  --per-occupation N     Stratified sampling: N tasks per occupation
  --gdpval-path PATH    Path to GDPVal parquet or directory
  --sectors SECTOR [...] Filter by sector(s)
  --occupations OCC [...]Filter by occupation(s)
```

### `list-tasks` — List available tasks

```
python -m gdpval_bench list-tasks [OPTIONS]

Options:
  --task-list PATH        Use a fixed task list JSON file
  --max-tasks N           Limit number of tasks
  --per-occupation N      Stratified sampling
  --sectors SECTOR [...]  Filter by sector(s)
  --occupations OCC [...] Filter by occupation(s)
```

## Task Data

### Bundled (50 tasks)

The repository ships with 50 tasks in `gdpval_bench/tasks_50_full.jsonl`, selected to cover all 44 occupations. These are loaded by default.

### Full Dataset (220 tasks)

To use all 220 tasks, install the HuggingFace `datasets` library:

```bash
pip install datasets
```

Tasks will be auto-downloaded from `openai/gdpval` when the bundled subset isn't sufficient.

### Task Structure

Each task contains:

```json
{
  "task_id": "uuid",
  "sector": "Health Care and Social Assistance",
  "occupation": "Nurse Practitioners",
  "prompt": "Full task description ...",
  "reference_files": ["path/to/file.pdf"],
  "task_value_usd": 50.0
}
```

## Result Format

### Run log (`run_log.jsonl`)

```json
{
  "task_id": "...",
  "run_name": "claude_v1",
  "timestamp": "2026-04-07T...",
  "status": "success",
  "return_code": 0,
  "elapsed_sec": 45.2,
  "output_tail": "... last 2000 chars of agent stdout ..."
}
```

### Per-task evaluation (`results.jsonl`)

```json
{
  "task_id": "...",
  "occupation": "Nurse Practitioners",
  "sector": "Health Care and Social Assistance",
  "task_value_usd": 50.0,
  "evaluation": {
    "has_evaluation": true,
    "evaluation_score": 0.75,
    "score_10": 7.5,
    "accepted": true,
    "artifact_count": 2,
    "artifact_paths": ["soap_note.pdf", "summary.txt"],
    "feedback": "..."
  },
  "eval_time_sec": 12.3,
  "timestamp": "2026-04-07T..."
}
```

### Summary (`summary.json`)

```json
{
  "total_tasks": 50,
  "evaluated": 50,
  "accepted": 35,
  "rejected": 15,
  "acceptance_rate": 0.7,
  "scores": {
    "mean": 6.8,
    "median": 7.0,
    "min": 2.0,
    "max": 9.5
  },
  "by_sector": { "...": { "count": 10, "mean_score": 7.2, "accepted": 8 } },
  "by_occupation": { "...": { "count": 1, "mean_score": 7.5 } }
}
```

## License

MIT
