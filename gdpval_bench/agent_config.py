"""Agent configuration loader for GDPVal Benchmark.

Reads agent_config.yaml and provides the command template, timeout,
concurrency, environment overrides, and sandbox settings for a named agent.
"""

from __future__ import annotations

import json
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
    output_format: str = "text"  # "text", "json", or "stream-json"
    agent_data_dir: Optional[str] = None  # raw value from config
    extra_writable_dirs: List[str] = field(default_factory=list)

    # Set by prepare() — absolute path to the resolved agent data dir
    resolved_data_dir: Optional[str] = field(default=None, repr=False)

    def prepare(self, workspace_root: str) -> None:
        """Resolve agent_data_dir under workspace_root and create it.

        Must be called once before running tasks. After this,
        ``resolved_data_dir`` holds the absolute path (or None).
        """
        if not self.agent_data_dir:
            self.resolved_data_dir = None
            return
        p = Path(workspace_root) / self.agent_data_dir
        p.mkdir(parents=True, exist_ok=True)
        self.resolved_data_dir = str(p.resolve())

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

        data_dir = self.resolved_data_dir or ""
        cmd = self.command.format(
            workspace=shlex.quote(workspace),
            prompt=shlex.quote(prompt),
            prompt_file=shlex.quote(prompt_file) if prompt_file else "",
            task_id=shlex.quote(task_id),
            task_json=shlex.quote(task_json),
            agent_data_dir=shlex.quote(data_dir) if data_dir else "",
            output_format=self.output_format,
        )
        return cmd, prompt_file

    def build_env(self, base_env: Dict[str, str]) -> Dict[str, str]:
        """Build the subprocess environment with placeholder expansion.

        Expands ``{agent_data_dir}`` in env values defined in the config.
        The caller should pass ``os.environ`` directly; this method copies it.
        """
        env = base_env.copy()
        data_dir = self.resolved_data_dir or ""
        for k, v in self.env.items():
            env[k] = v.replace("{agent_data_dir}", data_dir) if data_dir else v
        return env

    def parse_output(self, raw_output: str) -> str:
        """Extract the agent's text output, handling output_format.

        For ``output_format: json``, expects one or more JSON lines with a
        ``"text"`` field (e.g. ``{"type": "result", "text": "..."}``).
        The text values are concatenated. Falls back to raw output on
        parse failure.

        For ``output_format: stream-json``, expects newline-delimited JSON
        events from OpenHarness ``--output-format stream-json``.  Extracts
        assistant text from ``assistant_complete`` events and tool
        output from ``tool_completed`` events.
        """
        if self.output_format == "stream-json":
            return self._parse_stream_json(raw_output)

        if self.output_format != "json":
            return raw_output

        texts: list[str] = []
        for line in raw_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and "text" in obj:
                    texts.append(obj["text"])
            except json.JSONDecodeError:
                continue

        return "\n".join(texts) if texts else raw_output

    def _parse_stream_json(self, raw_output: str) -> str:
        """Parse stream-json output into readable text.

        Extracts assistant text and tool execution results from the
        structured event stream.
        """
        parts: list[str] = []
        for line in raw_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            evt_type = obj.get("type", "")
            if evt_type == "assistant_complete":
                text = obj.get("text", "").strip()
                if text:
                    parts.append(text)
            elif evt_type == "tool_completed":
                tool_name = obj.get("tool_name", "unknown")
                output = obj.get("output", "")
                is_error = obj.get("is_error", False)
                tag = "ERROR" if is_error else "output"
                parts.append(f"[{tool_name} {tag}] {output}")

        return "\n".join(parts) if parts else raw_output


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

    raw_data_dir = agent_data.get("agent_data_dir")

    return AgentConfig(
        name=agent_name,
        command=command.strip(),
        timeout=int(agent_data.get("timeout", 1800)),
        concurrency=max(1, int(agent_data.get("concurrency", 1))),
        env={str(k): str(v) for k, v in (agent_data.get("env") or {}).items()},
        use_bwrap=bool(agent_data.get("use_bwrap", False)),
        output_format=str(agent_data.get("output_format", "text")),
        agent_data_dir=str(raw_data_dir) if raw_data_dir else None,
        extra_writable_dirs=[
            os.path.expanduser(str(d))
            for d in (agent_data.get("extra_writable_dirs") or [])
        ],
    )


def _load_yaml(path: Path) -> Any:
    """Load a YAML file using PyYAML."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
