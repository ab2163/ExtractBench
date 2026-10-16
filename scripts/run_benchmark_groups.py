"""Run one pipeline over a dataset one split at a time (short, then medium, then long), stopping above a usage limit.

extract-bench skips documents that already have results, so re-running this command resumes an interrupted run.

Usage:
    uv run python scripts/run_benchmark_groups.py --pipeline <name> --input_dir data_all_with_boxes \
        --output_dir output_all_with_boxes --log_dir output/all_run --max_concurrent 10 --usage_limit 0.9
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_repeats import UsageProbe  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
GROUPS = ("short", "medium", "long")
CHECK_EVERY_S = 120


class GroupRunner:
    """Runs extract-bench on one split as its own process group, so it can be stopped as a whole."""

    def __init__(self, arguments: argparse.Namespace):
        self.arguments = arguments

    def start_group(self, group: str) -> subprocess.Popen:
        log_file = open(self.arguments.log_dir / f"{group}.log", "a")
        command = ["uv", "run", "extract-bench", "run", self.arguments.pipeline, "--input_dir",
                   str(self.arguments.input_dir), "--group", group, "--output_dir", str(self.arguments.output_dir),
                   "--max_concurrent", str(self.arguments.max_concurrent), "--open_report=False"]
        return subprocess.Popen(command, cwd=REPO, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)

    def stop_group(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return


class BenchmarkRunner:
    """Runs each split in turn, checking the 5-hour usage and stopping everything above the limit."""

    def __init__(self, arguments: argparse.Namespace):
        self.arguments = arguments
        self.group_runner = GroupRunner(arguments)
        self.probe = UsageProbe()

    def write_log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        print(line, flush=True)
        with open(self.arguments.log_dir / "run.log", "a") as handle:
            handle.write(line + "\n")

    def is_usage_too_high(self) -> bool:
        usage = self.probe.read_five_hour_usage()
        self.write_log(f"5-hour usage {usage}")
        return usage is not None and usage > self.arguments.usage_limit

    def run_group(self, group: str) -> bool:
        if self.is_usage_too_high():
            return False
        process = self.group_runner.start_group(group)
        self.write_log(f"started {group}")
        last_check = time.monotonic()
        while process.poll() is None:
            time.sleep(10)
            if time.monotonic() - last_check > CHECK_EVERY_S:
                last_check = time.monotonic()
                if self.is_usage_too_high():
                    self.group_runner.stop_group(process)
                    self.write_log(f"stopped {group}: usage limit reached")
                    return False
        self.write_log(f"finished {group} exit {process.returncode}")
        return True

    def run_all(self) -> None:
        self.arguments.log_dir.mkdir(parents=True, exist_ok=True)
        for group in self.arguments.groups:
            if not self.run_group(group):
                self.write_log("stopped: usage limit reached")
                return
        self.write_log("all groups done")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--input_dir", type=Path, required=True)
    parser.add_argument("--output_dir", type=Path, required=True)
    parser.add_argument("--log_dir", type=Path, required=True)
    parser.add_argument("--max_concurrent", type=int, default=10)
    parser.add_argument("--usage_limit", type=float, default=0.9)
    parser.add_argument("--groups", nargs="+", choices=GROUPS, default=list(GROUPS))
    BenchmarkRunner(parser.parse_args()).run_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
