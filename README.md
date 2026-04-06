# GDPVal Benchmark

A framework-agnostic evaluation pipeline for the **GDPVal** benchmark — 220 occupational tasks spanning 44 occupations across 9 sectors. GDPVal measures an agent's ability to produce grounded work artifacts (documents, spreadsheets, code, etc.) that meet professional standards.

This repository contains:

- **Benchmark tasks** — 220 tasks from the [OpenAI GDPVal dataset](https://huggingface.co/datasets/openai/gdpval) (50 bundled, full set downloadable from HuggingFace)
- **Evaluation rubrics** — 44 occupation-specific meta-prompts for LLM-based scoring
- **Evaluation pipeline** — CLI tool to score agent outputs against rubrics

You can evaluate **any** agent framework — CLI tools, API-based agents, multi-agent systems, or manual submissions.

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

You can use any of these configurations:

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
  {task_id}/
    task.json          # Task metadata + prompt
    reference_file.pdf # Downloaded reference files (if any)
    ...
```

### 4. Run your agent

Run your agent on each task. Your agent should:

1. Read the task prompt from `workspace/{task_id}/task.json`
2. (Optionally) use reference files in the same directory
3. Write output artifacts (files) into `workspace/{task_id}/`

**Example** — running a custom agent:

```python
import json
from pathlib import Path

workspace = Path("workspace")
for task_dir in sorted(workspace.iterdir()):
    if not task_dir.is_dir() or task_dir.name == "manifest.json":
        continue
    
    task = json.loads((task_dir / "task.json").read_text())
    prompt = task["augmented_prompt"]  # includes reference file locations
    
    # Run your agent here
    # result = my_agent.run(prompt, working_dir=str(task_dir))
    # Agent should write output files to task_dir
```

### 5. Evaluate

```bash
# Evaluate all tasks
python -m gdpval_bench evaluate --workspace workspace/

# Evaluate a single task
python -m gdpval_bench evaluate --workspace workspace/ --task-id <task_id>

# Resume interrupted evaluation
python -m gdpval_bench evaluate --workspace workspace/ --resume

# Name your run
python -m gdpval_bench evaluate --workspace workspace/ --run-name my_agent_v1
```

Results are saved to `gdpval_bench/results/<run_name>/`:

```
results/<run_name>/
  results.jsonl     # Per-task evaluation results
  summary.json      # Aggregate statistics
```

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

### Per-task results (`results.jsonl`)

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
  "timestamp": "2026-04-06T..."
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
