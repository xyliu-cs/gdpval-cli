#!/usr/bin/env python3
"""
GDPVal Benchmark — Framework-agnostic evaluation pipeline.

This module provides two main workflows:

  1. **Export tasks** — Write benchmark tasks to a directory so that any
     agent framework can consume them.

  2. **Evaluate** — Score agent-produced artifacts against occupation-specific
     rubrics (correctness only; no cost tracking).

Typical usage:

    # Step 1: Export tasks to a workspace
    python -m gdpval_bench export-tasks --output workspace/

    # Step 2: Run your agent on each task (outside this tool)
    #   Your agent should read the task prompt and reference files from
    #   workspace/{task_id}/ and write output artifacts there.

    # Step 3: Evaluate all tasks
    python -m gdpval_bench evaluate --workspace workspace/

    # Or evaluate a single task
    python -m gdpval_bench evaluate --workspace workspace/ --task-id <id>

Results are written to ``results/<run_name>/`` as JSONL + a summary JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Load .env if available ──
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from gdpval_bench.agent_config import load_agent_config
from gdpval_bench.sandbox import assert_bwrap_available, wrap_command
from gdpval_bench.task_loader import load_tasks, prepare_task_workspace, _iter_jsonl

# ── Default paths ──
_DEFAULT_RESULTS = Path(__file__).parent / "results"

# ── Evaluation constants ──
# Artifact extensions considered for evaluation
_ARTIFACT_EXTENSIONS = {
    '.pdf', '.docx', '.xlsx', '.pptx',       # documents
    '.txt', '.csv', '.json', '.md',           # text
    '.py', '.js', '.html', '.css',            # code
    '.png', '.jpg', '.jpeg', '.gif', '.webp', # images
}
# Minimum evaluation score to count as "accepted" (ClawWork-aligned cliff)
_MIN_EVALUATION_THRESHOLD = 0.6


# ═══════════════════════════════════════════════════════════════════
# Result I/O
# ═══════════════════════════════════════════════════════════════════

def _results_dir(run_name: str) -> Path:
    return _DEFAULT_RESULTS / run_name


def _append_jsonl(path: Path, record: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _load_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    return _iter_jsonl(path)


def _ref_filenames(task: Dict) -> List[str]:
    """Extract just the filenames from a task's reference_files list."""
    return [Path(rf).name for rf in (task.get("reference_files", []) or [])]


# ═══════════════════════════════════════════════════════════════════
# Artifact discovery
# ═══════════════════════════════════════════════════════════════════

def _discover_artifacts(
    workspace_dir: str,
    reference_filenames: List[str],
) -> List[str]:
    """Discover agent-created artifacts in the workspace.

    Scans ``workspace_dir`` for files matching ``_ARTIFACT_EXTENSIONS`` that
    are NOT the downloaded reference files.

    Returns:
        Sorted list of absolute paths to artifact files.
    """
    ws = Path(workspace_dir)
    if not ws.exists():
        return []

    ref_names = set(reference_filenames)
    artifacts: List[str] = []

    _EXCLUDED_DIRS = {
        ".pip_packages", "node_modules", "__pycache__", ".venv", "venv",
        ".git", ".tox", ".mypy_cache", ".pytest_cache", "dist-info",
    }

    for f in ws.rglob("*"):
        if not f.is_file():
            continue
        if _EXCLUDED_DIRS & set(f.relative_to(ws).parts):
            continue
        if f.suffix.lower() not in _ARTIFACT_EXTENSIONS:
            continue
        if f.name in ref_names:
            continue
        if f.stat().st_size == 0:
            continue
        artifacts.append(str(f))

    return sorted(artifacts)


# ═══════════════════════════════════════════════════════════════════
# Evaluator setup
# ═══════════════════════════════════════════════════════════════════

_evaluator_instance = None


def _get_evaluator():
    """Lazily create and cache the LLMEvaluator."""
    global _evaluator_instance
    if _evaluator_instance is not None:
        return _evaluator_instance

    meta_prompts_dir = Path(__file__).resolve().parent / "meta_prompts"
    if not meta_prompts_dir.exists():
        print(f"  Meta-prompts dir not found: {meta_prompts_dir}")
        return None

    try:
        from gdpval_bench.evaluator import LLMEvaluator
        evaluator = LLMEvaluator(
            meta_prompts_dir=str(meta_prompts_dir),
        )
        _evaluator_instance = evaluator
        return evaluator
    except Exception as e:
        print(f"  Could not initialize LLMEvaluator: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════
# Evaluate one task
# ═══════════════════════════════════════════════════════════════════

def _make_eval_result(
    feedback: str,
    artifact_paths: List[str] = (),
    evaluation_score: float = 0.0,
    has_evaluation: bool = False,
) -> Dict[str, Any]:
    """Build a standardized evaluation result dict."""
    accepted = evaluation_score >= _MIN_EVALUATION_THRESHOLD
    return {
        "has_evaluation": has_evaluation,
        "evaluation_score": round(evaluation_score, 4),
        "score_10": round(evaluation_score * 10, 1),
        "accepted": accepted,
        "artifact_count": len(artifact_paths),
        "artifact_paths": [os.path.basename(p) for p in artifact_paths],
        "feedback": feedback,
    }


def evaluate_task(
    task: Dict,
    workspace_dir: str,
) -> Dict[str, Any]:
    """Evaluate agent artifacts for a single task.

    Returns dict with keys:
        evaluation_score, score_10, accepted, artifact_paths, feedback, etc.
    """
    artifact_paths = _discover_artifacts(workspace_dir, _ref_filenames(task))

    if not artifact_paths:
        return _make_eval_result("No artifacts found in workspace.")

    evaluator = _get_evaluator()
    if evaluator is None:
        return _make_eval_result(
            "Evaluator not available (missing meta-prompts or API key).",
            artifact_paths,
        )

    max_payment = task.get("task_value_usd", 0.0) or 50.0

    try:
        evaluation_score, feedback, payment = evaluator.evaluate_artifact(
            task=task,
            artifact_paths=artifact_paths,
            description=f"Work submission with {len(artifact_paths)} artifact(s)",
            max_payment=max_payment,
        )
    except Exception as e:
        print(f"  Evaluation failed: {e}")
        return _make_eval_result(f"Evaluation error: {e}", artifact_paths)

    feedback_short = feedback[:500] + "..." if len(feedback) > 500 else feedback
    return _make_eval_result(
        feedback_short, artifact_paths,
        evaluation_score=evaluation_score, has_evaluation=True,
    )


# ═══════════════════════════════════════════════════════════════════
# Command: export-tasks
# ═══════════════════════════════════════════════════════════════════

def cmd_export_tasks(args: argparse.Namespace) -> None:
    """Export benchmark tasks to a workspace directory.

    Creates one subdirectory per task containing:
      - task.json   (task metadata + prompt)
      - reference files (downloaded from HuggingFace)
    """
    tasks = _load_filtered_tasks(args)
    if not tasks:
        print("No tasks loaded.")
        return

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nExporting {len(tasks)} tasks to {output_dir} ...\n")

    for i, task in enumerate(tasks, 1):
        tid = task["task_id"]
        task_dir = output_dir / tid
        task_dir.mkdir(parents=True, exist_ok=True)

        # Download reference files and get augmented prompt
        augmented_prompt = prepare_task_workspace(task, str(task_dir))

        # Write task metadata
        task_meta = {
            "task_id": tid,
            "occupation": task.get("occupation", ""),
            "sector": task.get("sector", ""),
            "prompt": task.get("prompt", ""),
            "augmented_prompt": augmented_prompt,
            "reference_files": _ref_filenames(task),
            "task_value_usd": task.get("task_value_usd", 0.0),
        }
        with open(task_dir / "task.json", "w", encoding="utf-8") as f:
            json.dump(task_meta, f, indent=2, ensure_ascii=False)

        ref_count = len(task.get("reference_files", []) or [])
        print(f"  [{i}/{len(tasks)}] {tid[:12]}... "
              f"({task.get('occupation', '?')}) "
              f"{'+ ' + str(ref_count) + ' ref files' if ref_count else ''}")

    # Write a manifest file
    manifest = {
        "exported_at": datetime.now().isoformat(),
        "task_count": len(tasks),
        "task_ids": [t["task_id"] for t in tasks],
        "occupations": sorted(set(t.get("occupation", "") for t in tasks)),
        "sectors": sorted(set(t.get("sector", "") for t in tasks)),
    }
    with open(output_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"\nDone. {len(tasks)} tasks exported to {output_dir}")
    print(f"Manifest written to {output_dir / 'manifest.json'}")
    print(f"\nNext step: run your agent on each task directory, then:")
    print(f"  python -m gdpval_bench evaluate --workspace {output_dir}")


# ═══════════════════════════════════════════════════════════════════
# Command: run
# ═══════════════════════════════════════════════════════════════════

def _run_single_task(
    task_dir: Path,
    agent_cfg: "AgentConfig",
    env: Dict[str, str],
    task_index: int,
    total: int,
) -> Dict[str, Any]:
    """Execute the registered CLI agent on a single task directory.

    Returns a result dict with status, return_code, elapsed, and output.
    """
    task_json_path = task_dir / "task.json"
    prompt_file: Optional[str] = None
    status = "error"
    return_code = -1
    output_tail = ""

    try:
        with open(task_json_path, "r", encoding="utf-8") as f:
            task_meta = json.load(f)
    except FileNotFoundError:
        return {"status": "skipped", "return_code": 0, "elapsed_sec": 0, "output_tail": ""}

    prompt = task_meta.get("augmented_prompt") or task_meta.get("prompt", "")
    task_id = task_meta.get("task_id", task_dir.name)
    workspace = str(task_dir.resolve())

    cmd, prompt_file = agent_cfg.build_command(
        workspace=workspace,
        prompt=prompt,
        task_id=task_id,
        task_json=str(task_json_path.resolve()),
    )
    final_cmd, cwd = wrap_command(cmd, workspace, agent_cfg.use_bwrap)

    occupation = task_meta.get("occupation", "?")
    print(f"  [{task_index}/{total}] {task_id[:12]}... ({occupation})", flush=True)
    print(f"           cmd: {final_cmd[:120]}{'...' if len(final_cmd) > 120 else ''}")

    t0 = time.monotonic()
    try:
        timeout = agent_cfg.timeout if agent_cfg.timeout > 0 else None
        proc = subprocess.run(
            final_cmd,
            shell=True,
            cwd=cwd,
            env=env,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        elapsed = time.monotonic() - t0
        status = "success" if proc.returncode == 0 else "error"
        return_code = proc.returncode
        raw_output = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        parsed_output = agent_cfg.parse_output(raw_output)
        output_tail = parsed_output[-2000:]
        print(f"           {status} (rc={return_code}, {elapsed:.1f}s)")
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        status = "timeout"
        print(f"           TIMEOUT after {elapsed:.0f}s")
    except Exception as e:
        elapsed = time.monotonic() - t0
        output_tail = str(e)
        print(f"           ERROR: {e}")
    finally:
        if prompt_file:
            try:
                os.unlink(prompt_file)
            except OSError:
                pass

    return {
        "status": status,
        "return_code": return_code,
        "elapsed_sec": round(elapsed, 2),
        "output_tail": output_tail,
    }


def cmd_run(args: argparse.Namespace) -> None:
    """Run the registered CLI agent on each task in the workspace.

    Reads agent_config.yaml, iterates over task directories, and
    launches the agent command per task with optional bwrap isolation.
    """
    workspace = Path(args.workspace).resolve()
    if not workspace.exists():
        print(f"Workspace not found: {workspace}")
        print("Run 'gdpval-bench export-tasks' first to create it.")
        return

    try:
        agent_cfg = load_agent_config(
            agent_name=getattr(args, "agent", None),
            config_path=getattr(args, "agent_config", None),
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"Agent config error: {e}")
        return

    if agent_cfg.use_bwrap:
        try:
            assert_bwrap_available()
        except RuntimeError as e:
            print(f"Sandbox error: {e}")
            return
        print("  Sandbox: bwrap isolation enabled")

    if agent_cfg.concurrency > 1:
        print(f"  Warning: concurrency={agent_cfg.concurrency} requested "
              f"but parallel execution is not yet implemented. Running sequentially.")

    manifest_path = workspace / "manifest.json"
    if manifest_path.exists():
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        task_ids = manifest.get("task_ids", [])
    else:
        # Fall back to scanning directories that contain task.json
        task_ids = sorted(
            d.name for d in workspace.iterdir()
            if d.is_dir() and (d / "task.json").exists()
        )

    if not task_ids:
        print("No task directories found in workspace.")
        return

    # Apply task-id filter if provided
    if args.task_id:
        if args.task_id not in task_ids:
            print(f"Task {args.task_id} not found in workspace.")
            return
        task_ids = [args.task_id]

    print(f"\nRunning agent '{agent_cfg.name}' on {len(task_ids)} tasks in {workspace}")
    print(f"  Agent command: {agent_cfg.command}")
    print(f"  Timeout: {agent_cfg.timeout}s")
    print(f"  Concurrency: {agent_cfg.concurrency}")
    print()

    # Run log
    run_name = args.run_name or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    rd = _results_dir(run_name)
    rd.mkdir(parents=True, exist_ok=True)
    run_log_file = rd / "run_log.jsonl"

    # Build the subprocess environment once
    env = os.environ.copy()
    env.update(agent_cfg.env)

    succeeded = 0
    failed = 0
    timed_out = 0
    skipped = 0

    for i, tid in enumerate(task_ids, 1):
        task_dir = workspace / tid
        if not task_dir.exists():
            print(f"  [{i}/{len(task_ids)}] {tid[:12]}... SKIPPED (dir not found)")
            skipped += 1
            continue

        result = _run_single_task(task_dir, agent_cfg, env, i, len(task_ids))

        # Log result
        record = {
            "task_id": tid,
            "run_name": run_name,
            "timestamp": datetime.now().isoformat(),
            **result,
        }
        _append_jsonl(run_log_file, record)

        if result["status"] == "success":
            succeeded += 1
        elif result["status"] == "timeout":
            timed_out += 1
        elif result["status"] == "skipped":
            skipped += 1
        else:
            failed += 1

    # Summary
    print(f"\n{'=' * 60}")
    print(f"  RUN SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Total:     {len(task_ids)}")
    print(f"  Succeeded: {succeeded}")
    print(f"  Failed:    {failed}")
    print(f"  Timed out: {timed_out}")
    print(f"  Skipped:   {skipped}")
    print(f"  Run log:   {run_log_file}")
    print(f"{'=' * 60}")
    print(f"\nNext step: evaluate the results:")
    print(f"  python -m gdpval_bench evaluate --workspace {workspace} --run-name {run_name}")


# ═══════════════════════════════════════════════════════════════════
# Command: evaluate
# ═══════════════════════════════════════════════════════════════════

def cmd_evaluate(args: argparse.Namespace) -> None:
    """Evaluate agent artifacts in a workspace directory.

    Expects workspace/{task_id}/ directories containing agent output files.
    Scores each task using occupation-specific rubrics and produces
    per-task results + an aggregate summary.
    """
    workspace = Path(args.workspace).resolve()
    if not workspace.exists():
        print(f"Workspace not found: {workspace}")
        return

    # Load task definitions (needed for rubric matching)
    tasks = _load_filtered_tasks(args)
    if not tasks:
        print("No tasks loaded.")
        return

    task_map = {t["task_id"]: t for t in tasks}

    # Determine which task directories to evaluate
    if args.task_id:
        task_ids = [args.task_id]
    else:
        # Find all task directories in workspace
        task_ids = []
        for d in sorted(workspace.iterdir()):
            if d.is_dir() and d.name in task_map:
                task_ids.append(d.name)

    if not task_ids:
        print("No matching task directories found in workspace.")
        print(f"  Workspace: {workspace}")
        print(f"  Expected: subdirectories named by task_id")
        return

    # Setup results directory
    run_name = args.run_name or datetime.now().strftime("eval_%Y%m%d_%H%M%S")
    rd = _results_dir(run_name)
    rd.mkdir(parents=True, exist_ok=True)
    results_file = rd / "results.jsonl"

    # Check for already-evaluated tasks (resume support)
    completed_ids = set()
    if args.resume:
        completed_ids = {r["task_id"] for r in _load_jsonl(results_file) if r.get("has_evaluation")}
        if completed_ids:
            print(f"Resuming: {len(completed_ids)} tasks already evaluated")

    print(f"\nEvaluating {len(task_ids)} tasks from {workspace}")
    print(f"Results: {rd}\n")

    results: List[Dict] = []
    evaluated = 0
    skipped = 0
    errors = 0

    for i, tid in enumerate(task_ids, 1):
        if tid in completed_ids:
            skipped += 1
            continue

        task = task_map.get(tid)
        if not task:
            print(f"  [{i}/{len(task_ids)}] {tid[:12]}... SKIPPED (no task definition)")
            continue

        task_dir = workspace / tid
        if not task_dir.exists():
            print(f"  [{i}/{len(task_ids)}] {tid[:12]}... SKIPPED (directory not found)")
            continue

        print(f"  [{i}/{len(task_ids)}] {task.get('occupation', '?')} ...", end=" ", flush=True)

        t0 = time.monotonic()
        eval_result = evaluate_task(task, str(task_dir))
        elapsed = time.monotonic() - t0

        record = {
            "task_id": tid,
            "occupation": task.get("occupation", ""),
            "sector": task.get("sector", ""),
            "task_value_usd": task.get("task_value_usd", 0.0),
            "evaluation": eval_result,
            "eval_time_sec": round(elapsed, 2),
            "timestamp": datetime.now().isoformat(),
        }

        _append_jsonl(results_file, record)
        results.append(record)

        if eval_result.get("has_evaluation"):
            evaluated += 1
            score = eval_result["score_10"]
            accepted = "ACCEPTED" if eval_result["accepted"] else "REJECTED"
            print(f"{score}/10 [{accepted}] ({elapsed:.1f}s)")
        else:
            errors += 1
            print(f"NO EVAL: {eval_result.get('feedback', '?')}")

    # Print + save summary
    _build_summary(results, rd, run_name, skipped)


def _build_summary(
    results: List[Dict],
    results_dir: Path,
    run_name: str,
    skipped: int,
) -> None:
    """Build and print aggregate evaluation summary."""
    evaluated = [r for r in results if r.get("evaluation", {}).get("has_evaluation")]
    if not evaluated:
        print("\nNo tasks were successfully evaluated.")
        return

    scores = [r["evaluation"]["score_10"] for r in evaluated]
    accepted = [r for r in evaluated if r["evaluation"]["accepted"]]
    rejected = [r for r in evaluated if not r["evaluation"]["accepted"]]

    # Per-sector breakdown
    sector_scores: Dict[str, List[float]] = {}
    for r in evaluated:
        sector = r.get("sector", "Unknown")
        sector_scores.setdefault(sector, []).append(r["evaluation"]["score_10"])

    # Per-occupation breakdown
    occ_scores: Dict[str, List[float]] = {}
    for r in evaluated:
        occ = r.get("occupation", "Unknown")
        occ_scores.setdefault(occ, []).append(r["evaluation"]["score_10"])

    summary = {
        "run_name": run_name,
        "timestamp": datetime.now().isoformat(),
        "total_tasks": len(results),
        "evaluated": len(evaluated),
        "skipped_resume": skipped,
        "accepted": len(accepted),
        "rejected": len(rejected),
        "acceptance_rate": round(len(accepted) / len(evaluated), 4),
        "acceptance_threshold": _MIN_EVALUATION_THRESHOLD,
        "scores": {
            "mean": round(sum(scores) / len(scores), 2),
            "median": round(sorted(scores)[len(scores) // 2], 2),
            "min": round(min(scores), 2),
            "max": round(max(scores), 2),
        },
        "by_sector": {
            sector: {
                "count": len(sc),
                "mean_score": round(sum(sc) / len(sc), 2),
                "accepted": sum(1 for s in sc if s / 10.0 >= _MIN_EVALUATION_THRESHOLD),
            }
            for sector, sc in sorted(sector_scores.items())
        },
        "by_occupation": {
            occ: {
                "count": len(sc),
                "mean_score": round(sum(sc) / len(sc), 2),
            }
            for occ, sc in sorted(occ_scores.items())
        },
    }

    with open(results_dir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    # Print
    print("\n" + "=" * 60)
    print("  EVALUATION SUMMARY")
    print("=" * 60)
    print(f"  Evaluated:       {len(evaluated)} tasks")
    print(f"  Accepted:        {len(accepted)} ({summary['acceptance_rate']:.0%})")
    print(f"  Rejected:        {len(rejected)}")
    print(f"  Acceptance cliff: score >= {_MIN_EVALUATION_THRESHOLD}")
    print(f"\n  Scores:")
    print(f"    Mean:   {summary['scores']['mean']}/10")
    print(f"    Median: {summary['scores']['median']}/10")
    print(f"    Min:    {summary['scores']['min']}/10")
    print(f"    Max:    {summary['scores']['max']}/10")

    if len(sector_scores) > 1:
        print(f"\n  By Sector:")
        for sector, data in sorted(summary["by_sector"].items()):
            print(f"    {sector}: {data['mean_score']}/10 "
                  f"({data['accepted']}/{data['count']} accepted)")

    print(f"\n  Results: {results_dir}")
    print("=" * 60)


# ═══════════════════════════════════════════════════════════════════
# Command: list-tasks
# ═══════════════════════════════════════════════════════════════════

def cmd_list_tasks(args: argparse.Namespace) -> None:
    """List available benchmark tasks."""
    tasks = _load_filtered_tasks(args)
    if not tasks:
        print("No tasks loaded.")
        return

    print(f"\n{'ID':<40} {'Occupation':<45} {'Sector'}")
    print("-" * 120)
    for t in tasks:
        tid = t["task_id"][:38]
        occ = t.get("occupation", "?")[:43]
        sec = t.get("sector", "?")
        print(f"  {tid:<38} {occ:<45} {sec}")

    print(f"\nTotal: {len(tasks)} tasks")
    occupations = set(t.get("occupation", "") for t in tasks)
    sectors = set(t.get("sector", "") for t in tasks)
    print(f"Occupations: {len(occupations)}, Sectors: {len(sectors)}")


# ═══════════════════════════════════════════════════════════════════
# Task loading helper
# ═══════════════════════════════════════════════════════════════════

def _load_filtered_tasks(args: argparse.Namespace) -> List[Dict]:
    """Load and filter tasks based on CLI args."""
    task_ids = None

    # Fixed task list file overrides filters
    task_list = getattr(args, "task_list", None)
    if task_list:
        tl_path = Path(task_list)
        if not tl_path.is_absolute():
            if tl_path.exists():
                tl_path = tl_path.resolve()
            else:
                tl_path = (Path(__file__).parent / tl_path).resolve()
        if not tl_path.exists():
            print(f"Task list file not found: {tl_path}")
            return []
        with open(tl_path, "r") as f:
            tl_data = json.load(f)
        task_ids = tl_data.get("task_ids", [])
        if not task_ids:
            print(f"No 'task_ids' array found in {tl_path}")
            return []
        print(f"Using fixed task list: {tl_path.name} ({len(task_ids)} tasks)")

    return load_tasks(
        gdpval_path=getattr(args, "gdpval_path", None),
        task_ids=task_ids,
        max_tasks=getattr(args, "max_tasks", None),
        sectors=getattr(args, "sectors", None),
        occupations=getattr(args, "occupations", None),
        per_occupation=getattr(args, "per_occupation", None),
    )


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add shared task-selection arguments to a subparser."""
    parser.add_argument("--task-list", type=str, default=None,
                        help="Path to a task-list JSON (must have 'task_ids' array). "
                             "Overrides other filters.")
    parser.add_argument("--max-tasks", type=int, default=None,
                        help="Max tasks to load (for testing)")
    parser.add_argument("--per-occupation", type=int, default=None,
                        help="Stratified sampling: N tasks per occupation")
    parser.add_argument("--gdpval-path", type=str, default=None,
                        help="Path to GDPVal parquet file or directory")
    parser.add_argument("--sectors", nargs="*", default=None,
                        help="Filter by sector name(s)")
    parser.add_argument("--occupations", nargs="*", default=None,
                        help="Filter by occupation name(s)")


def cli():
    parser = argparse.ArgumentParser(
        description="GDPVal Benchmark — framework-agnostic evaluation pipeline"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # ── export-tasks ──
    export_parser = subparsers.add_parser(
        "export-tasks",
        help="Export benchmark tasks to a workspace directory"
    )
    export_parser.add_argument(
        "--output", "-o", type=str, default="workspace",
        help="Output directory for exported tasks (default: workspace/)"
    )
    export_parser.add_argument(
        "--no-prefetch", action="store_true",
        help="Skip automatic reference file prefetch"
    )
    _add_common_args(export_parser)

    # ── run ──
    run_parser = subparsers.add_parser(
        "run",
        help="Run a CLI agent on each task in the workspace"
    )
    run_parser.add_argument(
        "--workspace", "-w", type=str, default="workspace",
        help="Path to workspace directory with exported tasks (default: workspace/)"
    )
    run_parser.add_argument(
        "--agent", type=str, default=None,
        help="Name of the agent to run (from agent_config.yaml)"
    )
    run_parser.add_argument(
        "--agent-config", type=str, default=None,
        help="Path to agent_config.yaml (default: auto-detect)"
    )
    run_parser.add_argument(
        "--task-id", type=str, default=None,
        help="Run agent on a single task by ID"
    )
    run_parser.add_argument(
        "--run-name", type=str, default=None,
        help="Name for this run (used for log directory)"
    )

    # ── evaluate ──
    eval_parser = subparsers.add_parser(
        "evaluate",
        help="Evaluate agent artifacts against rubrics"
    )
    eval_parser.add_argument(
        "--workspace", "-w", type=str, required=True,
        help="Path to workspace directory with task output subdirectories"
    )
    eval_parser.add_argument(
        "--task-id", type=str, default=None,
        help="Evaluate a single task by ID"
    )
    eval_parser.add_argument(
        "--run-name", type=str, default=None,
        help="Run name for results directory"
    )
    eval_parser.add_argument(
        "--resume", action="store_true",
        help="Skip already-evaluated tasks"
    )
    _add_common_args(eval_parser)

    # ── list-tasks ──
    list_parser = subparsers.add_parser(
        "list-tasks",
        help="List available benchmark tasks"
    )
    _add_common_args(list_parser)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    if args.command == "export-tasks":
        cmd_export_tasks(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "evaluate":
        cmd_evaluate(args)
    elif args.command == "list-tasks":
        cmd_list_tasks(args)


if __name__ == "__main__":
    cli()
