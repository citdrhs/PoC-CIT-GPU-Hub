"""Fit a line with repeatable gradient descent and resumable optimizer momentum."""
import argparse
import json
import time

from runner_sdk import Runner


def load(directory):
    s = json.loads((directory / "training.json").read_text(encoding="utf-8"))
    if s["format"] != 1 or s["step"] < 0 or s["learning_rate"] != 0.02:
        raise ValueError("Incompatible training checkpoint")
    return s


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--steps", type=int, default=250)
    parser.add_argument("--delay", type=float, default=0.05)
    args = parser.parse_args()
    with Runner() as runner:
        state = (load(runner.resume) if runner.resume else
                 dict(format=1, step=0, weight=0.0, velocity=0.0, learning_rate=0.02))
        runner.report("training_started", initial_step=state["step"])

        def write(directory, step):
            (directory/"training.json").write_text(json.dumps(state), encoding="utf-8")

        runner.register_checkpoint(write, load)
        while state["step"] < args.steps:
            gradient = 2 * (state["weight"] - 3)
            state["velocity"] = 0.8 * state["velocity"] + gradient
            state["weight"] -= state["learning_rate"] * state["velocity"]
            state["step"] += 1
            if not runner.boundary(state["step"], interval_steps=25):
                return
            time.sleep(args.delay)
        (runner.outputs/"training-result.json").write_text(json.dumps(state), encoding="utf-8")


if __name__ == "__main__":
    main()
