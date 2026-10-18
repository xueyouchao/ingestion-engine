"""A child process that takes a lease reservation and dies while still holding it.

The crash-recovery tests need a real process whose death is the only thing that frees the reservation:
that is what proves the reservation is an OS lock rather than a file a reader can be persuaded to
reclaim. It is a subprocess and not ``os.fork``: pytest runs multi-threaded here, and forking that is a
deadlock risk (and a ``DeprecationWarning`` since 3.12).

Both sides are bounded, so a child that never comes up fails the test instead of hanging it: the child
gives up after a deadline of its own, and the parent waits with a timeout. The child leaves with
``os._exit`` -- no ``finally``, no context-manager cleanup -- which is what a process killed mid-rent
leaves behind: a lock held by a process that no longer exists.

The program is a string rather than a module because it is that short, and because it must import
``deploy.gpu.contract`` from the repository being tested, not from whatever the child's cwd happens to be.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
DEADLINE_SECONDS = 20.0

# argv: repository, lease file, marker written once the lock is held, marker that tells the child to die,
# deadline as a monotonic-clock value. Exit 1 means died while holding; 2 means the deadline passed.
_HOLDER = """
import os, sys, time
from pathlib import Path

repository, lease, holding, die, deadline = sys.argv[1:6]
sys.path.insert(0, repository)
from deploy.gpu.contract import lease_reservation

lease, holding, die = Path(lease), Path(holding), Path(die)
with lease_reservation(lease):
    holding.write_text("holding", encoding="utf-8")
    while not die.exists():
        if time.monotonic() > float(deadline):
            os._exit(2)
        time.sleep(0.01)
os._exit(1)
"""


@dataclass
class Holder:
    """The running child, and the two marker files that tell it what to do."""

    process: subprocess.Popen[bytes]
    holding: Path
    die: Path

    def wait_until_holding(self, timeout: float = DEADLINE_SECONDS) -> None:
        """Block until the child has the reservation, so a later assertion is about the lock, not a race."""
        deadline = time.monotonic() + timeout
        while not self.holding.exists():
            if time.monotonic() > deadline:
                self.kill()
                raise AssertionError("the child never took the reservation")
            time.sleep(0.01)

    def die_holding(self, timeout: float = DEADLINE_SECONDS) -> int:
        """Tell the child to leave through ``os._exit(1)`` and return the status it exited with."""
        self.die.write_text("die", encoding="utf-8")
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise AssertionError("the child did not exit after being told to") from None

    def kill(self) -> None:
        self.process.kill()
        self.process.wait(timeout=DEADLINE_SECONDS)


def hold_reservation_then_die(lease: Path, directory: Path) -> Holder:
    """Start a child that holds ``lease``'s reservation until ``Holder.die_holding`` is called."""
    holding = directory / "child-holding"
    die = directory / "child-die"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLDER,
            str(REPOSITORY),
            str(lease),
            str(holding),
            str(die),
            str(time.monotonic() + DEADLINE_SECONDS),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return Holder(process=process, holding=holding, die=die)
