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

# Export all 220 tasks from the original HuggingFace dataset (see "Full Dataset" below)
python -m gdpval_bench export-tasks --output workspace/ --gdpval-path gdpval_data
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
  command: "claude -p {prompt} --output-format {output_format} --max-turns 30"
  output_format: text   # "text" (default), "json", or "stream-json"
  timeout: 1800         # Per-task timeout in seconds (0 = no timeout)
  concurrency: 1        # Reserved for future parallel execution
  use_bwrap: false      # Enable bwrap sandbox isolation (Linux only)
  env: {}               # Extra environment variables for the agent process

openharness:
  command: "openharness -p {prompt} --output-format {output_format} --max-turns 30"
  output_format: stream-json   # structured event stream with tool calls
  timeout: 1800
  concurrency: 1
  use_bwrap: true
  agent_data_dir: ".agent_data/openharness"   # relative to workspace root
  env:
    OPENHARNESS_CONFIG_DIR: "{agent_data_dir}"
    OPENHARNESS_DATA_DIR: "{agent_data_dir}/data"
    OPENHARNESS_LOGS_DIR: "{agent_data_dir}/logs"
  extra_writable_dirs:             # additional dirs the agent can write to
    - /path/to/OpenHarness         # e.g. the agent's own codebase
```

**`output_format`** controls how the agent's stdout is parsed:

- `text` (default) — stdout is used as-is.
- `json` — each line of stdout is parsed as JSON. Lines containing a `"text"` field (e.g. `{"type": "result", "text": "..."}`) have their text extracted and concatenated. This is compatible with OpenHands-style JSON output.
- `stream-json` — newline-delimited JSON event stream (e.g. OpenHarness `--output-format stream-json`). Extracts assistant text from `assistant_complete` events and tool output from `tool_completed` events. During execution, shows live previews of tool calls (`>> tool_name`) and results (`<< tool_name`).

**`agent_data_dir`** isolates agent framework data under the workspace:

Most agent frameworks store sessions, config, and logs in a home directory (e.g. `~/.openharness`). Setting `agent_data_dir` redirects this data to a subdirectory under the workspace root (e.g. `workspace/.agent_data/openharness/`), so benchmark runs never read or write to your personal agent installation. The `{agent_data_dir}` placeholder is available in both `command` and `env` values. The directory is auto-created and mounted read-write in the bwrap sandbox.

The agent framework itself must be installed on the host by the user (e.g. `pip install openharness`). Only its runtime data is redirected.

**Available placeholders** (usable in `command` and `env` values):

| Placeholder | Value |
|---|---|
| `{workspace}` | Absolute path to the task workspace directory (shell-escaped) |
| `{prompt}` | Full task prompt text (shell-escaped) |
| `{prompt_file}` | Path to a temp file containing the prompt (for long prompts) |
| `{task_id}` | The task ID string (shell-escaped) |
| `{task_json}` | Absolute path to the task's `task.json` file (shell-escaped) |
| `{agent_data_dir}` | Absolute path to the agent's isolated data directory |
| `{output_format}` | The configured `output_format` value (text, json, or stream-json) |

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
openharness:
  command: "openharness -p {prompt} --output-format json --max-turns 30"
  use_bwrap: true
  agent_data_dir: ".agent_data/openharness"
  env:
    OPENHARNESS_CONFIG_DIR: "{agent_data_dir}"
    OPENHARNESS_DATA_DIR: "{agent_data_dir}/data"
    OPENHARNESS_LOGS_DIR: "{agent_data_dir}/logs"
  extra_writable_dirs:
    - /path/to/OpenHarness
```

**Sandbox policy:**

- Host filesystem is mounted **read-only** (`--ro-bind / /`)
- The task workspace directory is mounted **read-write** (`--bind`)
- The `agent_data_dir` (if configured) is mounted **read-write** — isolated agent framework data
- Each path in `extra_writable_dirs` is mounted **read-write** — for agent harness codebases or other directories the agent needs to modify
- `/dev` and `/proc` are re-mounted for basic system access
- `TMPDIR` is redirected to the workspace (since `/tmp` is read-only)
- `PIP_TARGET` / `PYTHONPATH` point to `.pip_packages/` in the workspace
- Network is **not** isolated (agents may need HTTP access)
- `--die-with-parent` ensures cleanup if the parent process exits

The agent framework itself must be installed on the host by the user (e.g. `pip install openharness`). The benchmark only redirects its runtime data via environment variables so that each evaluation run gets a clean, isolated environment without touching your personal `~/.<agent>` directory.

**Requirements:**

```bash
# Install bubblewrap
sudo apt install bubblewrap

# Install your agent framework on the host
pip install openharness   # example — the benchmark does not manage this
```

This prevents agents from reading or writing files outside the workspace, giving each benchmark run a clean environment.

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

By default the CLI loads the bundled 50-task subset. To export all 220 tasks from the [original HuggingFace dataset](https://huggingface.co/datasets/openai/gdpval), download the dataset first and then point the CLI to it with `--gdpval-path`:

```bash
# 1. Install dependencies
pip install datasets pandas pyarrow

# 2. Download the dataset from HuggingFace
huggingface-cli download openai/gdpval --repo-type dataset --local-dir gdpval_data

# 3. Export all 220 tasks
python -m gdpval_bench export-tasks --output workspace/ --gdpval-path gdpval_data
```

You can also combine `--gdpval-path` with any of the filtering options:

```bash
# Export 220 tasks but only from specific sectors
python -m gdpval_bench export-tasks --output workspace/ --gdpval-path gdpval_data \
  --sectors "Health Care" "Information"

# Stratified sample: 3 tasks per occupation from the full dataset
python -m gdpval_bench export-tasks --output workspace/ --gdpval-path gdpval_data \
  --per-occupation 3
```

The `--gdpval-path` flag also works with `run`, `evaluate`, and `list-tasks` commands.

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
