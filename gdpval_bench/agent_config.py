"""Agent configuration loader for GDPVal Benchmark.

Reads agent_config.yaml and provides the command template, timeout,
concurrency, environment overrides, and sandbox settings.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


@dataclass
class AgentConfig:
    """Parsed agent configuration."""

    command: str
    timeout: int = 1800
    concurrency: int = 1
    env: Dict[str, str] = field(default_factory=dict)
    use_bwrap: bool = False

    def build_command(
        self,
        workspace: str,
        prompt: str,
        task_id: str,
        task_json: str,
    ) -> tuple[str, Optional[str]]:
        """Expand placeholders in the command template.

        Returns:
            (expanded_command, prompt_file_path_or_None)

        The caller is responsible for cleaning up the prompt file.
        """
        prompt_file: Optional[str] = None

        # Only create a prompt file if the template uses it
        if "{prompt_file}" in self.command:
            fd, prompt_file = tempfile.mkstemp(
                suffix=".txt", prefix="gdpval_prompt_", dir=workspace,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(prompt)

        cmd = self.command.format(
            workspace=shlex.quote(workspace),
            prompt=shlex.quote(prompt),
            prompt_file=shlex.quote(prompt_file) if prompt_file else "",
            task_id=shlex.quote(task_id),
            task_json=shlex.quote(task_json),
        )
        return cmd, prompt_file


def load_agent_config(config_path: str | Path | None = None) -> AgentConfig:
    """Load agent configuration from a YAML file.

    Search order:
      1. Explicit ``config_path`` argument
      2. ``agent_config.yaml`` in the current working directory
      3. ``agent_config.yaml`` next to this package (repo root)

    Raises ``FileNotFoundError`` if no config is found.
    """
    candidates = []
    if config_path:
        candidates.append(Path(config_path))
    candidates.append(Path.cwd() / "agent_config.yaml")
    candidates.append(Path(__file__).resolve().parent.parent / "agent_config.yaml")

    chosen: Optional[Path] = None
    for p in candidates:
        if p.exists():
            chosen = p
            break

    if chosen is None:
        raise FileNotFoundError(
            "No agent_config.yaml found. Create one in the current directory "
            "or pass --agent-config. See agent_config.yaml in the repo root "
            "for a template."
        )

    data = _load_yaml(chosen)
    if not isinstance(data, dict):
        raise ValueError(f"agent_config.yaml must be a YAML mapping, got {type(data).__name__}")

    command = data.get("command")
    if not command or not isinstance(command, str):
        raise ValueError("agent_config.yaml must define a 'command' string")

    return AgentConfig(
        command=command.strip(),
        timeout=int(data.get("timeout", 1800)),
        concurrency=max(1, int(data.get("concurrency", 1))),
        env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
        use_bwrap=bool(data.get("use_bwrap", False)),
    )


def _load_yaml(path: Path) -> Any:
    """Load a YAML file using PyYAML."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
