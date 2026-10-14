"""Run one extraction pipeline many times on one document, render each run's trace, and stop above a usage limit.

Each run goes to <runs_dir>/rNN/output, with its trace page in <runs_dir>/rNN. Runs that already have a result are
skipped, so the command can be re-run to finish an interrupted batch.

Usage:
    uv run python scripts/run_repeats.py --pipeline <name> --file data/short/x.pdf \
        --runs_dir traces/x/<mode> --first 1 --last 50 --parallel 5 --usage_limit 0.6
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class UsageProbe:
    """Reads the account's 5-hour usage from the rate-limit event of a tiny Claude call."""

    def read_five_hour_usage(self) -> float | None:
        command = ["claude", "--print", "--output-format", "stream-json", "--verbose", "--model", "claude-haiku-5-5",
                   "--no-session-persistence", "Reply with OK"]
        try:
            completed = subprocess.run(command, cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return None
        for line in completed.stdout.splitlines():
            if '"rate_limit_event"' in line:
                windows = json.loads(line).get("rate_limit_info", {}).get("unifiedWindows", {})
                return windows.get("five_hour", {}).get("utilization")
        return None


class RepeatRunner:
    """Starts runs up to the parallel limit, renders finished traces, and stops everything above the usage limit."""

    def __init__(self, arguments: argparse.Namespace):
        self.arguments = arguments
        self.probe = UsageProbe()
        self.running: dict[int, subprocess.Popen] = {}
        self.last_check = 0.0
        self.is_stopped = False

    def find_result(self, run_number: int) -> Path | None:
        matches = sorted(self.run_dir(run_number).glob("output/*/*/*.raw.json"))
        if matches:
            return matches[0]
        return None

    def run_dir(self, run_number: int) -> Path:
        return self.arguments.runs_dir / f"r{run_number:02d}"

    def start_run(self, run_number: int) -> None:
        run_dir = self.run_dir(run_number)
        run_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(run_dir / "run.log", "w")
        command = ["uv", "run", "extract-bench", "run", self.arguments.pipeline, "--file", str(self.arguments.file),
                   "--output_dir", str(run_dir / "output"), "--open_report=False"]
        self.running[run_number] = subprocess.Popen(command, cwd=REPO, stdout=log_file, stderr=subprocess.STDOUT,
                                                    start_new_session=True)
        self.write_log(f"started r{run_number:02d}")

    def finish_run(self, run_number: int) -> None:
        process = self.running.pop(run_number)
        result = self.find_result(run_number)
        if result is not None:
            subprocess.run([sys.executable, "scripts/render_trace.py", str(result), "--output_dir",
                            str(self.run_dir(run_number))], cwd=REPO, capture_output=True)
        self.write_log(f"finished r{run_number:02d} exit {process.returncode} result {result is not None}")

    def stop_everything(self) -> None:
        for run_number, process in self.running.items():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.write_log(f"stopped r{run_number:02d}")
        self.running = {}
        self.is_stopped = True

    def is_usage_too_high(self) -> bool:
        usage = self.probe.read_five_hour_usage()
        self.last_check = time.monotonic()
        self.write_log(f"5-hour usage {usage}")
        return usage is not None and usage > self.arguments.usage_limit

    def write_log(self, message: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)

    def list_pending(self) -> list[int]:
        pending = []
        for run_number in range(self.arguments.first, self.arguments.last + 1):
            if self.find_result(run_number) is None:
                pending.append(run_number)
        return pending

    def run_all(self) -> None:
        pending = self.list_pending()
        while not self.is_stopped and (pending or self.running):
            for run_number in list(self.running):
                if self.running[run_number].poll() is not None:
                    self.finish_run(run_number)
            if time.monotonic() - self.last_check > 120 and self.is_usage_too_high():
                self.stop_everything()
                break
            while pending and len(self.running) < self.arguments.parallel and not self.is_usage_too_high():
                self.start_run(pending.pop(0))
            if pending and len(self.running) < self.arguments.parallel:
                self.stop_everything()
            time.sleep(10)
        self.write_log("stopped: usage limit reached" if self.is_stopped else "all runs done")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--runs_dir", type=Path, required=True)
    parser.add_argument("--first", type=int, default=1)
    parser.add_argument("--last", type=int, default=50)
    parser.add_argument("--parallel", type=int, default=5)
    parser.add_argument("--usage_limit", type=float, default=0.6)
    RepeatRunner(parser.parse_args()).run_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
