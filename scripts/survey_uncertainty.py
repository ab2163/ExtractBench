"""Ask an agent to list where it is unsure how to extract a document, without doing the extraction.

The agent gets the same extraction task as the latest pipeline, but is told to stop after listing its value and box
uncertainties, the possible interpretations of each, and the reasons for and against each interpretation.

Usage:
    uv run python scripts/survey_uncertainty.py --file data/short/passcoag-2020-w2-p0003-r3.pdf \
        --output_dir traces/passcoag-2020-w2-p0003-r3/uncertainty/r01
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extract_bench.inference.providers.extract.claude_code_extract import ClaudeCodeExtractProvider  # noqa: E402
from extract_bench.inference.providers.extract.claude_code_uncertainty_extract import PromptComposer  # noqa: E402
from extract_bench.test_cases.loader import load_test_case  # noqa: E402

PIPELINE_CONFIG = {
    "model": "claude-opus-5-5",
    "effort": "high",
    "timeout_s": 1800,
    "evidence_mode": True,
    "is_checkbox_guidance": True,
    "is_checkbox_border": True,
    "is_yes_no_pair_rule": True,
    "is_all_fields_citation": True,
}

class SurveyPromptBuilder:
    """Builds the survey prompt around the exact extraction prompt the latest pipeline would send."""

    def __init__(self, provider: ClaudeCodeExtractProvider):
        self.provider = provider

    def build_prompt(self, pdf_path: Path) -> str:
        test_case = load_test_case(pdf_path)
        if test_case is None:
            raise ValueError(f"No test.json found next to {pdf_path}")
        schema = self.provider._prepare_schema(test_case.data_schema)
        extraction_prompt = self.provider._build_prompt(schema, "input.pdf")
        return PromptComposer().compose_survey_prompt(extraction_prompt)


class UncertaintySurveyor:
    """Runs the survey agent through the Claude Code CLI in a temporary folder holding only the document."""

    def __init__(self):
        self.provider = ClaudeCodeExtractProvider("claude_code_extract", dict(PIPELINE_CONFIG))
        self.prompt_builder = SurveyPromptBuilder(self.provider)

    def run_survey(self, pdf_path: Path) -> dict[str, Any]:
        prompt = self.prompt_builder.build_prompt(pdf_path)
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            shutil.copy2(pdf_path, workdir / "input.pdf")
            (workdir / "mcp_config.json").write_text('{"mcpServers": {}}')
            lines: list[str] = []
            command = self.provider._build_cmd(prompt, workdir / "mcp_config.json")
            self.provider._run_cli(command, workdir, pdf_path.stem, lines)
            uncertainties = self.read_uncertainties(workdir / "uncertainties.json")
            is_extraction_written = (workdir / "output.json").exists() or (workdir / "citations.json").exists()
        result_event = self.provider._parse_result_event(lines) or {}
        return {"prompt": prompt, "events": self.provider._parse_all_events(lines), "uncertainties": uncertainties,
                "cost_usd": result_event.get("total_cost_usd"), "seconds": round(time.monotonic() - started),
                "is_extraction_written": is_extraction_written}

    def read_uncertainties(self, path: Path) -> Any:
        if not path.exists():
            return {"error": "uncertainties.json not written"}
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as error:
            return {"error": f"uncertainties.json not valid JSON: {error}"}


class SurveyRecorder:
    """Saves the survey's findings, and its trace in the raw.json shape that render_trace.py reads."""

    def save_survey(self, survey: dict[str, Any], output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "uncertainties.json").write_text(json.dumps(survey["uncertainties"], indent=1))
        raw_output = {"_prompt": survey["prompt"], "_trace_full": survey["events"], "cost_usd": survey["cost_usd"],
                      "seconds": survey["seconds"], "is_extraction_written": survey["is_extraction_written"],
                      "_config": PIPELINE_CONFIG}
        (output_dir / "survey.raw.json").write_text(json.dumps({"raw_output": raw_output}))

    def summarize_survey(self, survey: dict[str, Any]) -> str:
        uncertainties = survey["uncertainties"]
        value_count = len(uncertainties.get("value_uncertainties") or [])
        box_count = len(uncertainties.get("box_uncertainties") or [])
        return (f"value uncertainties {value_count}, box uncertainties {box_count}, cost {survey['cost_usd']}, "
                f"{survey['seconds']} s, extraction written: {survey['is_extraction_written']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, required=True, help="document PDF with its .test.json alongside")
    parser.add_argument("--output_dir", type=Path, required=True)
    arguments = parser.parse_args()
    survey = UncertaintySurveyor().run_survey(arguments.file.resolve())
    recorder = SurveyRecorder()
    recorder.save_survey(survey, arguments.output_dir)
    print(recorder.summarize_survey(survey))
    return 0


if __name__ == "__main__":
    sys.exit(main())
