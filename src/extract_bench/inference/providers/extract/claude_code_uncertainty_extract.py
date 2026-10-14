"""Claude Code extraction that first lists the uncertainties in the task, then extracts with them in hand."""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from extract_bench.inference.providers.base import ProviderPermanentError
from extract_bench.inference.providers.extract.agent_evidence import (
    citations_from_agent_file,
    read_agent_citations_file,
)
from extract_bench.inference.providers.extract.claude_code_extract import ClaudeCodeExtractProvider
from extract_bench.inference.providers.extract.direct_model_utils import IMAGE_EXTENSIONS, page_count
from extract_bench.inference.providers.registry import register_provider
from extract_bench.schemas.pipeline import PipelineSpec
from extract_bench.schemas.pipeline_io import InferenceRequest, RawInferenceResult
from extract_bench.schemas.product import ProductType

UNCERTAINTIES_NAME = "uncertainties.json"
SHARED_SURVEY_CONFIG = {
    "model": "claude-opus-5-5",
    "effort": "high",
    "evidence_mode": True,
    "is_checkbox_guidance": True,
    "is_checkbox_border": True,
    "is_yes_no_pair_rule": True,
    "is_all_fields_citation": True,
    "is_full_trace": True,
    "survey_root": "output/surveys",
}

SURVEY_INTRO = """Below is an extraction task. It is your end goal, but do not carry it out now. Do not write
./output.json or ./citations.json. Instead, prepare for it as described after the task.

=== Extraction task (for reference only) ===
"""

SURVEY_STEPS = """=== End of extraction task ===

What to do now, instead of the extraction:
1. Consider the schema and the document ./input.pdf. You may use tools and think as you normally would.
2. Identify specific schema values where you are uncertain which value to pick from the document.
3. Identify specific citations where you are uncertain where the evidence box should be placed, or what its
   boundaries should be.
4. For each uncertainty from steps 2 and 3, list the different interpretations you could make.
5. For each interpretation, list (a) reasons why it is sensible and (b) reasons why it is not sensible.

Then stop. Write your findings to ./uncertainties.json in this shape:
{"value_uncertainties": [{"field_path": "...", "page": 1, "question": "...",
  "interpretations": [{"interpretation": "...", "reasons_sensible": ["..."], "reasons_not_sensible": ["..."]}]}],
 "box_uncertainties": [{"field_path": "...", "page": 1, "question": "...",
  "interpretations": [{"interpretation": "...", "reasons_sensible": ["..."], "reasons_not_sensible": ["..."]}]}]}
Use the field paths from the schema, with a bracketed index for list rows. Do not print the JSON."""

FOLLOW_UP_PROMPT = """Now carry out the extraction task from my first message, exactly as it describes: write
./output.json and ./citations.json. Your uncertainty assessment is in ./uncertainties.json."""

INFORMED_RULE = """
- The current directory also contains ./uncertainties.json: an assessment by a previous agent of the
  uncertainties in this extraction. It lists schema values and evidence boxes that agent was unsure about, the
  possible interpretations of each, and reasons why each interpretation is or is not sensible."""


class PromptComposer:
    """Builds the survey, follow-up and uncertainty-informed prompts around the standard extraction prompt."""

    def compose_survey_prompt(self, extraction_prompt: str) -> str:
        return SURVEY_INTRO + extraction_prompt + "\n\n" + SURVEY_STEPS

    def compose_follow_up_prompt(self) -> str:
        return FOLLOW_UP_PROMPT

    def compose_informed_prompt(self, extraction_prompt: str) -> str:
        return extraction_prompt + INFORMED_RULE


class UncertaintyExtractBase(ClaudeCodeExtractProvider):
    """Shared machinery for extraction runs made of several Claude Code phases in one or two working folders."""

    MODE = "base"

    def __init__(self, provider_name: str, base_config: dict[str, Any] | None = None):
        super().__init__(provider_name, base_config)
        self.composer = PromptComposer()

    def build_phase_command(self, prompt: str, workdir: Path, resume_session: str | None, is_kept: bool,
                            is_forked: bool) -> list[str]:
        command = self._build_cmd(prompt, workdir / "mcp_config.json")
        if is_kept:
            command.remove("--no-session-persistence")
        if resume_session:
            separator = command.index("--")
            command[separator:separator] = ["--resume", resume_session]
        if resume_session and is_forked:
            separator = command.index("--")
            command[separator:separator] = ["--fork-session"]
        return command

    def run_phase(self, name: str, prompt: str, workdir: Path, example_id: str, resume_session: str | None,
                  is_kept: bool = False, is_forked: bool = False) -> dict:
        lines: list[str] = []
        command = self.build_phase_command(prompt, workdir, resume_session, is_kept, is_forked)
        returncode, stderr_tail = self._run_cli(command, workdir, example_id, lines)
        result_event = self._parse_result_event(lines)
        self._raise_for_status(returncode, result_event, lines, stderr_tail)
        return {"name": name, "prompt": prompt, "lines": lines, "result": result_event or {},
                "session_id": self.find_session_id(lines)}

    def find_session_id(self, lines: list[str]) -> str | None:
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("session_id"):
                return event["session_id"]
        return None

    def stage_workdir(self, workdir: Path, source: Path) -> str:
        staged_name = f"input{source.suffix.lower()}"
        shutil.copy2(source, workdir / staged_name)
        (workdir / "mcp_config.json").write_text('{"mcpServers": {}}')
        return staged_name

    def read_uncertainties(self, workdir: Path) -> Any:
        path = workdir / UNCERTAINTIES_NAME
        if not path.exists():
            raise ProviderPermanentError(f"{self.MODE}: the survey phase did not write ./{UNCERTAINTIES_NAME}.")
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as error:
            raise ProviderPermanentError(f"{self.MODE}: ./{UNCERTAINTIES_NAME} is not valid JSON: {error}") from error

    def check_request(self, request: InferenceRequest) -> Path:
        if request.product_type != ProductType.EXTRACT:
            raise ProviderPermanentError(f"{type(self).__name__} only supports EXTRACT, got {request.product_type}")
        if not request.schema_override:
            raise ProviderPermanentError("schema_override is required for EXTRACT product type.")
        source = Path(request.source_file_path)
        if not source.exists():
            raise ProviderPermanentError(f"File not found: {source}")
        if source.suffix.lower() != ".pdf" and source.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ProviderPermanentError(f"{type(self).__name__} supports PDFs and images, got {source.suffix}")
        return source

    def collect_extraction(self, workdir: Path, staged_name: str) -> dict[str, Any]:
        outcome = {"data": self._read_output_json(workdir), "citations": None, "files": None}
        if self._evidence_mode:
            outcome["citations"] = read_agent_citations_file(workdir / "citations.json")
        if self._is_full_trace:
            outcome["files"] = self._read_workdir_texts(workdir, staged_name)
        return outcome

    def run_phases(self, source: Path, schema: dict[str, Any], example_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def merge_phase_lines(self, phases: list[dict]) -> list[str]:
        merged: list[str] = []
        for phase in phases:
            merged.append(json.dumps({"type": "system", "subtype": "phase", "phase": phase["name"]}))
            merged.extend(phase["lines"])
        return merged

    def sum_phase_costs(self, phases: list[dict]) -> float | None:
        total = 0.0
        for phase in phases:
            cost = phase["result"].get("total_cost_usd")
            if cost is None:
                return None
            total = total + cost
        return total

    def build_raw_output(self, outcome: dict[str, Any], num_pages: int) -> dict[str, Any]:
        phases = outcome["phases"]
        lines = self.merge_phase_lines(phases)
        cost_usd = self.sum_phase_costs(phases)
        raw_output: dict[str, Any] = {
            "data": outcome["data"], "model": self._model, "num_pages": num_pages, "cost_usd": cost_usd,
            "cost_per_page_usd": cost_usd / num_pages if cost_usd and num_pages else None,
            "phase_costs_usd": self.list_phase_costs(phases), "uncertainties": outcome["uncertainties"],
            "usage": phases[-1]["result"].get("usage") or {}, "phase_usage": self.list_phase_usage(phases),
            "survey_reference": outcome.get("survey_reference"),
            "_config": self._config_snapshot(), **self._tool_usage(lines), "_trace_tail": self._trace_tail(lines),
        }
        if self._is_full_trace:
            raw_output["_prompt"] = self.join_prompts(phases)
            raw_output["_trace_full"] = self._parse_all_events(lines)
            raw_output["_workdir_files"] = outcome["files"]
        if self._evidence_mode:
            citations, stats = citations_from_agent_file(outcome["citations"], source="claude_code_evidence")
            raw_output["field_citations"] = [citation.model_dump() for citation in citations]
            raw_output["evidence_stats"] = stats.as_dict()
        return raw_output

    def list_phase_costs(self, phases: list[dict]) -> dict[str, Any]:
        costs: dict[str, Any] = {}
        for phase in phases:
            costs[phase["name"]] = phase["result"].get("total_cost_usd")
        return costs

    def list_phase_usage(self, phases: list[dict]) -> dict[str, Any]:
        usage: dict[str, Any] = {}
        for phase in phases:
            usage[phase["name"]] = phase["result"].get("usage") or {}
        return usage

    def join_prompts(self, phases: list[dict]) -> str:
        parts = []
        for phase in phases:
            parts.append(f"===== Prompt for phase: {phase['name']} =====\n{phase['prompt']}")
        return "\n\n".join(parts)

    def _config_snapshot(self) -> dict[str, Any]:
        snapshot = super()._config_snapshot()
        snapshot["mode"] = self.MODE
        return snapshot

    def run_inference(self, pipeline: PipelineSpec, request: InferenceRequest) -> RawInferenceResult:
        source = self.check_request(request)
        started_at = datetime.now()
        schema = self._prepare_schema(request.schema_override)
        outcome = self.run_phases(source, schema, request.example_id)
        completed_at = datetime.now()
        raw_output = self.build_raw_output(outcome, page_count(source))
        return RawInferenceResult(
            request=request, pipeline=pipeline, pipeline_name=pipeline.pipeline_name,
            product_type=request.product_type, raw_output=raw_output, started_at=started_at,
            completed_at=completed_at, latency_in_ms=int((completed_at - started_at).total_seconds() * 1000),
        )


@register_provider("claude_code_survey_then_extract")
class SurveyThenExtractProvider(UncertaintyExtractBase):
    """One agent lists its uncertainties first, then the same agent (resumed session) does the extraction."""

    MODE = "survey_then_extract"

    def run_phases(self, source: Path, schema: dict[str, Any], example_id: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            staged_name = self.stage_workdir(workdir, source)
            extraction_prompt = self._build_prompt(schema, staged_name)
            survey_prompt = self.composer.compose_survey_prompt(extraction_prompt)
            survey = self.run_phase("survey", survey_prompt, workdir, example_id, None, is_kept=True)
            uncertainties = self.read_uncertainties(workdir)
            if not survey["session_id"]:
                raise ProviderPermanentError(f"{self.MODE}: no session id in the survey phase to resume.")
            follow_up = self.run_phase("extraction", self.composer.compose_follow_up_prompt(), workdir, example_id,
                                       survey["session_id"])
            outcome = self.collect_extraction(workdir, staged_name)
        outcome["phases"] = [survey, follow_up]
        outcome["uncertainties"] = uncertainties
        return outcome


@register_provider("claude_code_uncertainty_informed_extract")
class UncertaintyInformedExtractProvider(UncertaintyExtractBase):
    """One agent lists its uncertainties; a fresh agent then extracts with that list in its working folder."""

    MODE = "uncertainty_informed_extract"

    def run_survey(self, source: Path, schema: dict[str, Any], example_id: str) -> tuple[dict, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            staged_name = self.stage_workdir(workdir, source)
            survey_prompt = self.composer.compose_survey_prompt(self._build_prompt(schema, staged_name))
            survey = self.run_phase("survey", survey_prompt, workdir, example_id, None)
            return survey, self.read_uncertainties(workdir)

    def run_phases(self, source: Path, schema: dict[str, Any], example_id: str) -> dict[str, Any]:
        survey, uncertainties = self.run_survey(source, schema, example_id)
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            staged_name = self.stage_workdir(workdir, source)
            (workdir / UNCERTAINTIES_NAME).write_text(json.dumps(uncertainties, indent=1))
            informed_prompt = self.composer.compose_informed_prompt(self._build_prompt(schema, staged_name))
            extraction = self.run_phase("extraction", informed_prompt, workdir, example_id, None)
            outcome = self.collect_extraction(workdir, staged_name)
        outcome["phases"] = [survey, extraction]
        outcome["uncertainties"] = uncertainties
        return outcome


SCRATCH_GLOBS = ("/private/tmp/claude-*/*/{session}", "/tmp/claude-*/*/{session}")


class SharedSurveyStore:
    """Keeps one saved uncertainty survey per document: its record, working folder and folder snapshots."""

    def __init__(self, root: Path, stem: str):
        self.folder = (root / stem).resolve()
        self.record_path = self.folder / "survey.json"
        self.workdir = self.folder / "workdir"
        self.snapshot = self.folder / "workdir_snapshot"
        self.scratch_snapshot = self.folder / "scratch_snapshot"
        self.lock_path = self.folder / "resume.lock"

    def load_record(self) -> dict[str, Any]:
        if not self.record_path.exists():
            raise ProviderPermanentError(f"No shared survey at {self.record_path}; run scripts/make_shared_survey.py")
        return json.loads(self.record_path.read_text())

    def save_record(self, record: dict[str, Any]) -> None:
        self.record_path.write_text(json.dumps(record, indent=1))

    def find_scratch_dir(self, session_id: str) -> Path | None:
        import glob

        for pattern in SCRATCH_GLOBS:
            matches = glob.glob(pattern.format(session=session_id))
            if matches:
                return Path(matches[0])
        return None

    def take_snapshots(self, session_id: str) -> str | None:
        shutil.copytree(self.workdir, self.snapshot)
        scratch_dir = self.find_scratch_dir(session_id)
        if scratch_dir is None:
            return None
        shutil.copytree(scratch_dir, self.scratch_snapshot)
        return str(scratch_dir)

    def restore_snapshots(self, record: dict[str, Any]) -> None:
        self.replace_folder(self.workdir, self.snapshot)
        if record.get("scratch_dir") and self.scratch_snapshot.exists():
            self.replace_folder(Path(record["scratch_dir"]), self.scratch_snapshot)

    def replace_folder(self, target: Path, source: Path) -> None:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)


class SharedSurveyMaker(UncertaintyExtractBase):
    """Runs the uncertainty survey once in a permanent folder, keeps its session, and snapshots the result."""

    MODE = "shared_survey"

    def make_survey(self, source: Path, schema: dict[str, Any], store: SharedSurveyStore) -> dict[str, Any]:
        if store.folder.exists():
            raise ProviderPermanentError(f"A shared survey already exists at {store.folder}")
        store.workdir.mkdir(parents=True)
        staged_name = self.stage_workdir(store.workdir, source)
        survey_prompt = self.composer.compose_survey_prompt(self._build_prompt(schema, staged_name))
        survey = self.run_phase("survey", survey_prompt, store.workdir, source.stem, None, is_kept=True)
        uncertainties = self.read_uncertainties(store.workdir)
        record = {"session_id": survey["session_id"], "workdir": str(store.workdir), "staged_name": staged_name,
                  "uncertainties": uncertainties, "cost_usd": survey["result"].get("total_cost_usd"),
                  "usage": survey["result"].get("usage") or {}, "config": self._config_snapshot()}
        record["scratch_dir"] = store.take_snapshots(survey["session_id"])
        store.save_record(record)
        trace = {"_prompt": survey_prompt, "_trace_full": self._parse_all_events(survey["lines"]), **record}
        (store.folder / "survey.raw.json").write_text(json.dumps({"raw_output": trace}))
        return record


@register_provider("claude_code_resume_survey_extract")
class ResumeSurveyExtractProvider(UncertaintyExtractBase):
    """Forks the saved survey session and asks that same agent, with its full context, to do the extraction."""

    MODE = "resume_shared_survey"

    def run_phases(self, source: Path, schema: dict[str, Any], example_id: str) -> dict[str, Any]:
        import fcntl

        store = SharedSurveyStore(Path(self.base_config.get("survey_root", "output/surveys")), Path(example_id).name)
        record = store.load_record()
        with open(store.lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            store.restore_snapshots(record)
            extraction = self.run_phase("extraction", self.composer.compose_follow_up_prompt(), store.workdir,
                                        example_id, record["session_id"], is_forked=True)
            outcome = self.collect_extraction(store.workdir, record["staged_name"])
        outcome["phases"] = [extraction]
        outcome["uncertainties"] = record["uncertainties"]
        outcome["survey_reference"] = {"session_id": record["session_id"], "survey_cost_usd": record["cost_usd"]}
        return outcome


@register_provider("claude_code_fresh_survey_extract")
class FreshSurveyExtractProvider(UncertaintyExtractBase):
    """Gives a fresh agent the standard task plus the saved survey's uncertainties.json, with no other context."""

    MODE = "fresh_with_shared_survey"

    def run_phases(self, source: Path, schema: dict[str, Any], example_id: str) -> dict[str, Any]:
        store = SharedSurveyStore(Path(self.base_config.get("survey_root", "output/surveys")), Path(example_id).name)
        record = store.load_record()
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            staged_name = self.stage_workdir(workdir, source)
            (workdir / UNCERTAINTIES_NAME).write_text(json.dumps(record["uncertainties"], indent=1))
            informed_prompt = self.composer.compose_informed_prompt(self._build_prompt(schema, staged_name))
            extraction = self.run_phase("extraction", informed_prompt, workdir, example_id, None)
            outcome = self.collect_extraction(workdir, staged_name)
        outcome["phases"] = [extraction]
        outcome["uncertainties"] = record["uncertainties"]
        outcome["survey_reference"] = {"session_id": record["session_id"], "survey_cost_usd": record["cost_usd"]}
        return outcome
