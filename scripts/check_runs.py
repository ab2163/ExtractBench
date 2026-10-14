"""Ask a checker agent whether each extraction run made genuine, unambiguous mistakes, and record its YES/NO verdicts.

The checker gets the document, a text transcript of the extraction run, and the run's output.json and citations.json.

Usage:
    uv run python scripts/check_runs.py --runs_glob 'traces/cabrera-2021/runs/r*' --pdf data/medium/cabrera-2021.pdf \
        --results output/checker/cabrera.jsonl --parallel 5 --usage_limit 0.6
"""

from __future__ import annotations

import argparse
import glob
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extract_bench.inference.providers.extract.claude_code_extract import ClaudeCodeExtractProvider  # noqa: E402

MODEL = "claude-opus-5-5"
EFFORT = "high"
TIMEOUT_S = 1800
MAX_RESULT_CHARS = 4000

CHECKER_PROMPT = """You are checking the work of another agent that extracted data from a document for a benchmark.

Files in the current directory:
- ./input.pdf: the document.
- ./trace.txt: a text transcript of the extraction agent's session. It starts with the exact instructions that
  agent was given, including the JSON schema and the rules for evidence boxes. Images the agent looked at
  appear as [image]; long tool outputs are shortened.
- ./output.json: the data the agent extracted.
- ./citations.json: the agent's evidence boxes. Each has "field_path", "page" (1-indexed) and "bbox" as [x, y,
  width, height] fractions of the page, origin at the top-left corner.

Check thoroughly. You may render pages, crop them and draw the agent's boxes on them to look. Use only local
files and tools; no network. Refer to the prompt provided to the agent in the trace to judge whether mistakes
were made against it.

Write ./verdict.json as {"verdict": "YES" or "NO", "mistakes": [{"field_path": "...", "kind": "value" or
"box", "reason": "..."}]}. YES means at least one genuine, unambiguous mistake; NO means none (and an empty
mistakes list). Do not print the JSON."""


class TraceCondenser:
    """Turns a run's saved stream-json events into a plain-text transcript without embedded images."""

    def condense_trace(self, raw_output: dict[str, Any]) -> str:
        parts = ["=== Instructions given to the extraction agent ===", raw_output.get("_prompt", "")]
        parts.extend(["", "=== Session ==="])
        for event in raw_output.get("_trace_full") or []:
            if isinstance(event, dict) and event.get("type") in ("assistant", "user"):
                self.add_message(event, parts)
        return "\n".join(parts)

    def add_message(self, event: dict[str, Any], parts: list[str]) -> None:
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                parts.append(f"[agent message]\n{block.get('text', '')}")
            elif block_type == "tool_use":
                parts.append(f"[tool call: {block.get('name')}]\n{self.describe_input(block.get('input') or {})}")
            elif block_type == "tool_result":
                parts.append(f"[tool result]\n{self.describe_result(block.get('content'))}")

    def describe_input(self, tool_input: dict[str, Any]) -> str:
        if "command" in tool_input:
            return str(tool_input["command"])
        return json.dumps(tool_input)

    def describe_result(self, content: Any) -> str:
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
                elif isinstance(item, dict):
                    texts.append("[image]")
            content = "\n".join(texts)
        text = str(content)
        if len(text) > MAX_RESULT_CHARS:
            return text[:MAX_RESULT_CHARS] + f"\n...[{len(text) - MAX_RESULT_CHARS} more characters]"
        return text


class CheckerWorkspace:
    """Stages the document, transcript and the run's outputs into a folder for the checker."""

    def __init__(self, condenser: TraceCondenser):
        self.condenser = condenser

    def stage_files(self, workdir: Path, pdf_path: Path, raw_output: dict[str, Any]) -> None:
        shutil.copy2(pdf_path, workdir / "input.pdf")
        (workdir / "trace.txt").write_text(self.condenser.condense_trace(raw_output), encoding="utf-8")
        (workdir / "output.json").write_text(json.dumps(raw_output.get("data"), indent=1), encoding="utf-8")
        citations = []
        for citation in raw_output.get("field_citations") or []:
            citations.append({"field_path": citation.get("field_path"), "page": citation.get("page"),
                              "bbox": citation.get("bbox")})
        (workdir / "citations.json").write_text(json.dumps(citations, indent=1), encoding="utf-8")
        (workdir / "mcp_config.json").write_text('{"mcpServers": {}}')


class RunChecker:
    """Runs one checker agent through the Claude Code CLI and reads back its verdict."""

    def __init__(self, workspace: CheckerWorkspace):
        self.workspace = workspace
        self.provider = ClaudeCodeExtractProvider("claude_code_extract",
                                                  {"model": MODEL, "effort": EFFORT, "timeout_s": TIMEOUT_S})

    def check_run(self, run_dir: Path, pdf_path: Path) -> dict[str, Any]:
        raw_path = sorted(run_dir.glob("output/*/*/*.raw.json"))[0]
        raw_output = json.loads(raw_path.read_text())["raw_output"]
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            self.workspace.stage_files(workdir, pdf_path, raw_output)
            lines: list[str] = []
            command = self.provider._build_cmd(CHECKER_PROMPT, workdir / "mcp_config.json")
            self.provider._run_cli(command, workdir, run_dir.name, lines)
            verdict = self.read_verdict(workdir / "verdict.json")
        result_event = self.provider._parse_result_event(lines) or {}
        verdict["run"] = run_dir.name
        verdict["cost_usd"] = result_event.get("total_cost_usd")
        verdict["seconds"] = round(time.monotonic() - started)
        verdict["checker_trace"] = self.provider._parse_all_events(lines)
        return verdict

    def read_verdict(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"verdict": None, "mistakes": [], "error": "verdict.json not written"}
        try:
            verdict = json.loads(path.read_text())
        except json.JSONDecodeError as error:
            return {"verdict": None, "mistakes": [], "error": f"verdict.json not valid JSON: {error}"}
        return {"verdict": verdict.get("verdict"), "mistakes": verdict.get("mistakes") or []}


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


class CheckerStudy:
    """Checks every run without a saved verdict, in parallel, and stops starting checks above a usage limit."""

    def __init__(self, results_path: Path, usage_limit: float):
        self.results_path = results_path
        self.usage_limit = usage_limit
        self.checker = RunChecker(CheckerWorkspace(TraceCondenser()))
        self.probe = UsageProbe()
        self.lock = threading.Lock()
        self.is_stopped = False

    def load_done_runs(self) -> set[str]:
        done: set[str] = set()
        if not self.results_path.exists():
            return done
        for line in self.results_path.read_text().splitlines():
            record = json.loads(line)
            if record.get("verdict") in ("YES", "NO"):
                done.add(record["run"])
        return done

    def check_and_save(self, run_dir: Path, pdf_path: Path) -> None:
        if self.is_usage_too_high():
            return
        try:
            verdict = self.checker.check_run(run_dir, pdf_path)
        except Exception as error:
            verdict = {"run": run_dir.name, "verdict": None, "mistakes": [], "error": repr(error)}
        with self.lock:
            with open(self.results_path, "a") as handle:
                handle.write(json.dumps(verdict) + "\n")
        print(f"{run_dir.name}: {verdict.get('verdict')} cost {verdict.get('cost_usd')}", flush=True)

    def is_usage_too_high(self) -> bool:
        with self.lock:
            if self.is_stopped:
                return True
            usage = self.probe.read_five_hour_usage()
            print(f"5-hour usage {usage}", flush=True)
            if usage is not None and usage > self.usage_limit:
                self.is_stopped = True
            return self.is_stopped

    def run_study(self, run_dirs: list[Path], pdf_path: Path, parallel: int) -> None:
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        done = self.load_done_runs()
        pending = []
        for run_dir in run_dirs:
            if run_dir.name not in done:
                pending.append(run_dir)
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            for run_dir in pending:
                pool.submit(self.check_and_save, run_dir, pdf_path)
        print("stopped: usage limit reached" if self.is_stopped else "all checks done", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs_glob", required=True, help="glob of run folders, each holding output/*/*/*.raw.json")
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True, help="JSONL of verdicts; saved ones are kept")
    parser.add_argument("--parallel", type=int, default=5)
    parser.add_argument("--usage_limit", type=float, default=0.6)
    arguments = parser.parse_args()
    run_dirs = []
    for path in sorted(glob.glob(arguments.runs_glob)):
        run_dirs.append(Path(path))
    CheckerStudy(arguments.results, arguments.usage_limit).run_study(run_dirs, arguments.pdf, arguments.parallel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
