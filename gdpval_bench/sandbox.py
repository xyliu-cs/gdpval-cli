"""Bubblewrap (bwrap) sandbox for isolated agent execution.

Provides filesystem isolation per task: the host filesystem is mounted
read-only while the task workspace is the only writable directory.

Modeled after OpenSpace-GDPval's local_connector.py implementation.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil


def assert_bwrap_available() -> None:
    """Abort if bwrap is not installed or not on Linux."""
    if platform.system() != "Linux":
        raise RuntimeError(
            "bwrap isolation requires Linux. "
            "Set use_bwrap: false in agent_config.yaml on other platforms."
        )
    if shutil.which("bwrap") is None:
        raise RuntimeError(
            "bwrap (bubblewrap) is not found on PATH. "
            "Install it (e.g. 'apt install bubblewrap') or set "
            "use_bwrap: false in agent_config.yaml."
        )


def bwrap_shell_prefix(working_dir: str) -> str:
    """Build a shell-safe bwrap prefix for ``subprocess.run(shell=True)``.

    Sandbox policy:
      * Host filesystem mounted read-only (``--ro-bind / /``)
      * ``working_dir`` mounted read-write (task workspace)
      * ``/dev`` and ``/proc`` re-mounted for basic system access
      * ``TMPDIR`` set to workspace (``/tmp`` is read-only)
      * ``PIP_TARGET`` / ``PYTHONPATH`` pointed at ``.pip_packages``
        inside the workspace so ``pip install --user`` works
      * Network is **not** isolated (agents may need HTTP)
      * ``--die-with-parent`` ensures cleanup on parent exit
    """
    ws = shlex.quote(working_dir)
    pip_target = shlex.quote(os.path.join(working_dir, ".pip_packages"))
    return (
        f"bwrap --ro-bind / / --bind {ws} {ws} "
        f"--dev /dev --proc /proc "
        f"--setenv TMPDIR {ws} "
        f"--setenv PIP_TARGET {pip_target} "
        f"--setenv PYTHONPATH {pip_target} "
        f"--chdir {ws} "
        f"--die-with-parent -- "
    )


def wrap_command(command: str, working_dir: str, use_bwrap: bool) -> tuple[str, str]:
    """Optionally wrap a shell command with bwrap.

    Returns:
        (final_command, cwd_for_subprocess)
    """
    if use_bwrap:
        return bwrap_shell_prefix(working_dir) + command, "/"
    return command, working_dir
