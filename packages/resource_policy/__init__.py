"""Versioned, fixed half-slot limits. All capacity arithmetic is integral."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Envelope:
    memory_bytes: int
    cpu_quota_us: int
    cpu_period_us: int
    pids_max: int = 256
    warning_percent: int = 85
    stop_percent: int = 90
    gpu_fraction: str | None = None
    swap_bytes: int = 0
    policy_version: str = "fixed_half_v1"

    def as_dict(self):
        return asdict(self)


def fixed_half(memory_bytes, reserve_bytes, logical_cpus, reserved_cpus=0, *,
               alignment_bytes=1 << 20, period_us=100_000, gpu=False):
    for value in (memory_bytes, reserve_bytes, logical_cpus, reserved_cpus,
                  alignment_bytes, period_us):
        if type(value) is not int or value < 0:
            raise ValueError("Inventory, reserve and alignment must be nonnegative integers")
    if not alignment_bytes or not period_us:
        raise ValueError("Alignment and quota period must be positive")
    memory = (memory_bytes - reserve_bytes) // (2 * alignment_bytes) * alignment_bytes
    quota = (logical_cpus - reserved_cpus) * period_us // 2
    if memory <= 0 or quota < 1000:
        raise ValueError("Insufficient student capacity after host reserves")
    return Envelope(memory, quota, period_us, gpu_fraction="1/2" if gpu else None)
