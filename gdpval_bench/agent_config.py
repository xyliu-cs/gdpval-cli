"""Agent configuration loader for GDPVal Benchmark.

Reads agent_config.yaml and provides the command template, timeout,
concurrency, environment overrides, and sandbox settings for a named agent.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class AgentConfig:
    """Parsed agent configuration."""

    name: str
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


def _find_config_file(config_path: str | Path | None = None) -> Path:
    """Locate agent_config.yaml.

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

    for p in candidates:
        if p.exists():
            return p

    raise FileNotFoundError(
        "No agent_config.yaml found. Create one in the current directory "
        "or pass --agent-config. See agent_config.yaml in the repo root "
        "for a template."
    )


def list_agents(config_path: str | Path | None = None) -> List[str]:
    """Return the names of all agents defined in the config file."""
    chosen = _find_config_file(config_path)
    data = _load_yaml(chosen)
    if not isinstance(data, dict):
        raise ValueError(f"agent_config.yaml must be a YAML mapping, got {type(data).__name__}")
    return list(data.keys())


def load_agent_config(
    agent_name: str | None = None,
    config_path: str | Path | None = None,
) -> AgentConfig:
    """Load a named agent configuration from agent_config.yaml.

    If ``agent_name`` is None and the config contains exactly one agent,
    that agent is used. Otherwise an error is raised listing available agents.

    Raises ``FileNotFoundError`` if no config file is found.
    Raises ``ValueError`` for invalid config content.
    """
    chosen = _find_config_file(config_path)
    data = _load_yaml(chosen)

    if not isinstance(data, dict):
        raise ValueError(f"agent_config.yaml must be a YAML mapping, got {type(data).__name__}")

    if not data:
        raise ValueError("agent_config.yaml is empty — define at least one agent")

    # Resolve which agent to use
    if agent_name is None:
        if len(data) == 1:
            agent_name = next(iter(data))
        else:
            names = ", ".join(data.keys())
            raise ValueError(
                f"Multiple agents defined in config ({names}). "
                f"Use --agent <name> to select one."
            )

    if agent_name not in data:
        names = ", ".join(data.keys())
        raise ValueError(
            f"Agent '{agent_name}' not found in config. Available: {names}"
        )

    agent_data = data[agent_name]
    if not isinstance(agent_data, dict):
        raise ValueError(f"Agent '{agent_name}' must be a YAML mapping")

    command = agent_data.get("command")
    if not command or not isinstance(command, str):
        raise ValueError(f"Agent '{agent_name}' must define a 'command' string")

    return AgentConfig(
        name=agent_name,
        command=command.strip(),
        timeout=int(agent_data.get("timeout", 1800)),
        concurrency=max(1, int(agent_data.get("concurrency", 1))),
        env={str(k): str(v) for k, v in (agent_data.get("env") or {}).items()},
        use_bwrap=bool(agent_data.get("use_bwrap", False)),
    )


def _load_yaml(path: Path) -> Any:
    """Load a YAML file using PyYAML."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
