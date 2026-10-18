"""Running an external tool, and proving it produced what was expected."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class CommandFailed(RuntimeError):
    """Raised when an external tool failed or is not installed."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    """Runs a command and reports what happened. Tests substitute a recording runner.

    ``environ`` is extra environment for *this* invocation, not a replacement for the process's own.
    A GPU provider uses it to give one lease's start and stop commands the same identity, so the two
    commands can find the one worker they own without a shared file (ADR-0007).
    """

    def run(
        self,
        argv: Sequence[str],
        cwd: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class SubprocessRunner:
    """The real runner: no shell, bounded time, captured output."""

    def __init__(self, timeout_seconds: float = 1800.0) -> None:
        self._timeout = timeout_seconds

    def run(
        self,
        argv: Sequence[str],
        cwd: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> CommandResult:
        executable = argv[0]
        if shutil.which(executable) is None:
            raise CommandFailed(f"{executable!r} is not installed or not on PATH")
        try:
            completed = subprocess.run(
                list(argv),
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                # Extra variables are added to this process's environment, never a replacement for it:
                # a start command still needs PATH, HOME and the vendor credentials it was configured
                # with, and only the lease identity is added.
                env=None if environ is None else {**os.environ, **environ},
            )
        except subprocess.TimeoutExpired as error:
            raise CommandFailed(f"{executable} did not finish within {self._timeout:.0f}s: {' '.join(argv)}") from error
        return CommandResult(
            argv=tuple(argv),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def require_success(result: CommandResult) -> None:
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        raise CommandFailed(f"{result.argv[0]} exited with {result.returncode}: {' | '.join(tail)}")
