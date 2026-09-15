"""Resource telemetry for local inference, without utilization limits or pacing."""

from __future__ import annotations

import platform
import re
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field

import psutil


def apple_gpu_percent() -> float | None:
    """Read the Apple GPU driver sensor without sudo or a helper daemon."""
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(  # noqa: S603
            ["/usr/sbin/ioreg", "-r", "-c", "IOAccelerator", "-l"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        values = re.findall(
            r'"Device Utilization %"\s*=\s*(\d+(?:\.\d+)?)', result.stdout
        )
        return max(map(float, values)) if values else None
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass
class ResourceSample:
    timestamp: float
    cpu_percent: float | None = None
    gpu_percent: float | None = None
    device: str = "unknown"
    rss_bytes: int = 0


@dataclass
class InferenceResourceController:
    """Record actual CPU/GPU use; never throttle or reject inference."""

    sample_interval: float = 0.25
    gpu_sensor: Callable[[], float | None] = apple_gpu_percent
    cpu_sensor: Callable[[], float | None] | None = None
    samples: list[ResourceSample] = field(default_factory=list)
    jobs: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        if self.sample_interval <= 0:
            raise ValueError("Sampling interval must be positive")

    @property
    def cpu_threads(self) -> int:
        return psutil.cpu_count() or 1

    def sample(self, device: str = "unknown") -> ResourceSample:
        cpu = (
            self.cpu_sensor() if self.cpu_sensor else psutil.cpu_percent(interval=0.05)
        )
        sample = ResourceSample(
            time.time(),
            cpu_percent=cpu,
            gpu_percent=self.gpu_sensor(),
            device=device,
            rss_bytes=psutil.Process().memory_info().rss,
        )
        with self._lock:
            self.samples.append(sample)
        return sample

    def admit(self, device: str = "unknown") -> ResourceSample:
        """Compatibility hook: measure utilization without admission limits."""
        return self.sample(device)

    @contextmanager
    def measure(self, device: str, *, job: str):
        """Monitor a synchronous operation, including failures, until completion."""
        self.admit(device)
        start = time.monotonic()
        stop = threading.Event()
        errors: list[str] = []

        def monitor():
            while not stop.wait(self.sample_interval):
                try:
                    self.sample(device)
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
                    return

        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        status = "failed"
        try:
            yield
            status = "completed"
        finally:
            stop.set()
            thread.join(timeout=3)
            self.sample(device)
            self.jobs.append(
                {
                    "job": job,
                    "device": device,
                    "elapsed_s": time.monotonic() - start,
                    "status": status,
                    "concurrency": 1,
                    "sensor_errors": errors,
                }
            )

    def report(self) -> dict:
        def extrema(values):
            values = [v for v in values if v is not None]
            return (
                {"average": sum(values) / len(values), "peak": max(values)}
                if values
                else None
            )

        missing = any(
            s.cpu_percent is None or s.gpu_percent is None for s in self.samples
        )
        errors = any(j["sensor_errors"] for j in self.jobs)
        elapsed = sum(j["elapsed_s"] for j in self.jobs)
        return {
            "limit_percent": None,
            "throttling": False,
            "sample_interval_s": self.sample_interval,
            "sample_count": len(self.samples),
            "max_concurrency": 1,
            "device": sorted({s.device for s in self.samples}),
            "cpu_percent": extrema([s.cpu_percent for s in self.samples]),
            "gpu_percent": extrema([s.gpu_percent for s in self.samples]),
            "rss_peak_bytes": max((s.rss_bytes for s in self.samples), default=0),
            "sensor_missing": missing,
            "status": "measured"
            if self.samples and not (missing or errors)
            else "incomplete",
            "elapsed_s": elapsed,
            "jobs": self.jobs,
            "samples": [asdict(s) for s in self.samples],
            "policy": "Telemetry only; utilization does not gate inference",
        }
