"""Runtime profile & hardware budget detection (spec §1.1, §11.1).

Depends on: psutil, ``core.config``. Used by: ``bot.app`` (boot), ``core.settings`` (auto values),
``compute.pool`` (worker count), ``core.shutdown`` (max uptime), startup report, ``/stats``.

Public API
----------
- ``detect(config) -> RuntimeInfo`` — profile, ephemeral flag, effective CPUs, RAM budget, auto sizes.
- ``RuntimeInfo.auto_workers(reserve_mb, per_worker_mb, max_workers)``

Never raises: unreadable cgroup files fall back to psutil values.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

from bot.core.config import Config


@dataclass(frozen=True, slots=True)
class RuntimeInfo:
    profile: str  # persistent | colab | github_actions | windows_dev
    ephemeral: bool
    os_name: str
    python: str
    cpus: int  # effective CPUs (cgroup quota ∩ affinity ∩ cores)
    ram_total_mb: int  # cgroup limit if present, else physical RAM
    use_uvloop: bool

    def auto_workers(self, reserve_mb: int = 512, per_worker_mb: int = 300, max_workers: int = 8) -> int:
        """clamp(min(cpus − 1, (available − reserve) // per_worker), 1, max) — never more workers than CPUs."""
        avail = available_ram_mb(self)
        by_ram = (avail - reserve_mb) // max(per_worker_mb, 1)
        return int(max(1, min(self.cpus - 1, by_ram, max_workers)))

    def auto_light(self) -> int:
        return max(2, min(2 * self.cpus, 16))

    def auto_heavy(self, workers: int) -> int:
        return max(2, min(workers + 1, 8))


def detect(config: Config) -> RuntimeInfo:
    """Detect where the bot runs and what hardware it may use."""
    profile = config.runtime_profile or _guess_profile()
    ephemeral = (profile in ("colab", "github_actions")) if config.data_persistent is None else not config.data_persistent
    return RuntimeInfo(
        profile=profile,
        ephemeral=ephemeral,
        os_name=f"{platform.system()} {platform.release()}",
        python=sys.version.split()[0],
        cpus=_effective_cpus(),
        ram_total_mb=_ram_limit_mb(),
        use_uvloop=os.name != "nt",
    )


def _guess_profile() -> str:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "github_actions"
    if os.environ.get("COLAB_RELEASE_TAG") or "google.colab" in sys.modules:
        return "colab"
    if os.name == "nt":
        return "windows_dev"
    return "persistent"


def _effective_cpus() -> int:
    cores = os.cpu_count() or 1
    try:
        cores = min(cores, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass
    quota = _cgroup_cpu_quota()
    return max(1, min(cores, quota) if quota else cores)


def _cgroup_cpu_quota() -> int | None:
    """cgroup v2 ``cpu.max`` → whole CPUs (``max`` = unlimited → None)."""
    try:
        q, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        return None if q == "max" else max(1, int(int(q) / int(period)))
    except (OSError, ValueError):
        return None


def _ram_limit_mb() -> int:
    total = psutil.virtual_memory().total // 2**20
    try:
        raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if raw != "max":
            return int(min(total, int(raw) // 2**20))
    except (OSError, ValueError):
        pass
    return int(total)


def available_ram_mb(info: RuntimeInfo | None = None) -> int:
    """Free RAM for new work: cgroup budget − cgroup usage when limited, else system available."""
    sys_avail = psutil.virtual_memory().available // 2**20
    try:
        cur = int(Path("/sys/fs/cgroup/memory.current").read_text()) // 2**20
        limit_raw = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if limit_raw != "max":
            return int(min(sys_avail, int(limit_raw) // 2**20 - cur))
    except (OSError, ValueError):
        pass
    return int(sys_avail)
