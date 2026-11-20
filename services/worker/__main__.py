"""Local worker daemon and read-only hardware discovery."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from packages.execution_backends.builds import RECIPE
from packages.execution_backends.docker import CpuContainerBackend, DellMigBackend, RuntimeUnavailable, SimulatedGpuBackend
from packages.observability import fingerprint, probe
from services.scheduler.store import Store
from .agent import Worker


def demo_inventory():
    inventory = {"memory_bytes": 128 << 30, "logical_cpus": 20, "architecture": "fixture",
                 "execution_mode": "fixture", "image_digest": "simulated-fixture-v1",
                 "build_recipe": "simulated-build-v1", "memory_model": "illustrative",
                 "note": "Generated demonstration inventory; source and CUDA are not executed."}
    inventory["fingerprint"] = fingerprint(inventory)
    return inventory


def provision(args):
    evidence = None
    backend = None
    if args.backend == "demo":
        inventory = demo_inventory()
        reserve = (16 << 30) if args.reserve_bytes is None else args.reserve_bytes
        cpus = 4 if args.reserve_cpus is None else args.reserve_cpus
    else:
        inventory = probe()
        if args.reserve_bytes is None or args.reserve_cpus is None:
            raise ValueError("Native worker requires explicit --reserve-bytes and --reserve-cpus after inspecting the host")
        reserve, cpus = args.reserve_bytes, args.reserve_cpus
        backend_cls = CpuContainerBackend if args.backend == "dell-mig" else SimulatedGpuBackend
        backend = backend_cls(args.node, args.image, inventory)
        inventory.update(image_digest=backend.image, execution_mode="docker", build_recipe=RECIPE)
        inventory["fingerprint"] = fingerprint([inventory["fingerprint"], backend.image, RECIPE, reserve, cpus])
        if args.backend == "dell-mig":
            if not args.evidence:
                raise ValueError(f"Dell requires operator qualification for fingerprint {inventory['fingerprint']}")
            evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            backend.mig = DellMigBackend(evidence)
            backend.mig.validate_profile(inventory)
    if args.demo_seconds < 1:
        raise ValueError("--demo-seconds must be positive")
    store = Store(args.db)
    worker = Worker(store, args.root, args.node, backend, args.demo_seconds)
    worker.reserve = reserve
    datasets = ["numbers-v1"] if (worker.storage.root / "datasets" / "numbers-v1").is_dir() else []
    store.register_node(args.node, args.name, "dell-mig" if args.backend == "dell-mig" else "simulated",
                        inventory, reserve, reserved_cpus=cpus, evidence=evidence, dataset_ids=datasets,
                        environments=["python-cpu-v1"])
    return worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="cmd", required=True)
    commands.add_parser("probe", help="Report current inventory without altering host or GPU configuration")
    run = commands.add_parser("run")
    run.add_argument("--backend", choices=("demo", "docker", "dell-mig"), required=True)
    run.add_argument("--node", default="cit-development")
    run.add_argument("--name", default="CIT Development")
    run.add_argument("--db", default=os.environ.get("GPUHUB_DB", ".gpuhub/hub.sqlite3"))
    run.add_argument("--root", default=os.environ.get("GPUHUB_DATA", ".gpuhub/data"))
    run.add_argument("--demo-seconds", default=180, type=int)
    run.add_argument("--image", default=os.environ.get("GPUHUB_IMAGE", "cit-gpu-hub:local"))
    run.add_argument("--reserve-bytes", default=os.environ.get("GPUHUB_RESERVE_BYTES"), type=int)
    run.add_argument("--reserve-cpus", default=os.environ.get("GPUHUB_RESERVE_CPUS"), type=int)
    run.add_argument("--evidence", type=Path)
    run.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.cmd == "probe":
        print(json.dumps(probe(), indent=2))
        return
    try:
        worker = provision(args)
    except (OSError, ValueError, RuntimeUnavailable) as exc:
        parser.exit(2, f"Worker startup refused: {exc}\n")
    try:
        while True:
            worker.tick()
            if args.once:
                break
            time.sleep(.35)
    except KeyboardInterrupt:
        pass
    finally:
        worker.journal.close()


if __name__ == "__main__":
    main()
