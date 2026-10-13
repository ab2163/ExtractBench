"""Agentic extraction through a direct Anthropic Messages API tool-use loop, with a settable temperature."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from extract_bench.inference.providers.base import (
    ProviderConfigError,
    ProviderPermanentError,
    ProviderRateLimitError,
    ProviderTransientError,
)
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

SYSTEM_PROMPT = (
    "You are an agent working in a local directory. Use the bash tool to run shell commands "
    "and the view_image tool to look at image files you create, such as rendered PDF pages."
)
MAX_TOOL_OUTPUT_CHARS = 30000
BASH_TIMEOUT_S = 300
MAX_IMAGE_BYTES = 4_500_000
IMAGE_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
}
PRICE_PER_M = {"claude-opus-4-6": (5.00, 25.00), "claude-opus-4-5": (5.00, 25.00), "claude-sonnet-4-6": (3.00, 15.00)}
CACHE_WRITE_RATE = 1.25
CACHE_READ_RATE = 0.10
RETRY_WAIT_S = 20
MAX_API_RETRIES = 4


class ShellRunner:
    """Runs one bash command inside the agent's working folder and returns its combined output."""

    def __init__(self, workdir: Path):
        self.workdir = workdir

    def run_command(self, command: str) -> tuple[str, bool]:
        try:
            completed = subprocess.run(
                ["bash", "-c", command], cwd=self.workdir, capture_output=True, text=True, timeout=BASH_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            return f"Command timed out after {BASH_TIMEOUT_S} s.", True
        output = completed.stdout + completed.stderr
        if completed.returncode != 0:
            output = output + f"\n[exit code {completed.returncode}]"
        return self.truncate_output(output), completed.returncode != 0

    def truncate_output(self, output: str) -> str:
        if len(output) <= MAX_TOOL_OUTPUT_CHARS:
            return output if output else "(no output)"
        hidden = len(output) - MAX_TOOL_OUTPUT_CHARS
        return output[:MAX_TOOL_OUTPUT_CHARS] + f"\n...[{hidden} more characters not shown]"


class ImageReader:
    """Loads an image file from the working folder as a Messages API image block."""

    def __init__(self, workdir: Path):
        self.workdir = workdir

    def read_image(self, relative_path: str) -> tuple[dict[str, Any] | None, str]:
        path = (self.workdir / relative_path).resolve()
        media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None or not path.is_file():
            return None, f"Cannot view {relative_path}: not an existing png, jpg, webp or gif file."
        payload = path.read_bytes()
        if len(payload) > MAX_IMAGE_BYTES:
            payload = self.shrink_to_jpeg(path)
            media_type = "image/jpeg"
        block = {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                              "data": base64.b64encode(payload).decode("ascii")}}
        return block, f"[image {relative_path}, {len(payload)} bytes]"

    def shrink_to_jpeg(self, path: Path) -> bytes:
        from io import BytesIO

        from PIL import Image

        image = Image.open(path).convert("RGB")
        quality = 90
        while True:
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            if buffer.tell() <= MAX_IMAGE_BYTES or quality <= 40:
                return buffer.getvalue()
            quality = quality - 15


class AgentToolbox:
    """Defines the agent's tools and turns each tool_use block into a tool_result block."""

    TOOLS = [
        {"name": "bash", "description": "Run a bash command in the working directory and return stdout and stderr.",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "view_image", "description": "Look at a png, jpg, webp or gif file in the working directory.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    ]

    def __init__(self, workdir: Path):
        self.shell_runner = ShellRunner(workdir)
        self.image_reader = ImageReader(workdir)

    def run_tool(self, block: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        tool_input = block.get("input") or {}
        if block["name"] == "bash":
            output, is_error = self.shell_runner.run_command(str(tool_input.get("command", "")))
            result = self.build_result(block["id"], [{"type": "text", "text": output}], is_error)
            return result, result
        if block["name"] == "view_image":
            return self.run_view_image(block["id"], str(tool_input.get("path", "")))
        error = self.build_result(block["id"], [{"type": "text", "text": f"Unknown tool {block['name']}."}], True)
        return error, error

    def run_view_image(self, tool_use_id: str, relative_path: str) -> tuple[dict[str, Any], dict[str, Any]]:
        image_block, summary = self.image_reader.read_image(relative_path)
        if image_block is None:
            error = self.build_result(tool_use_id, [{"type": "text", "text": summary}], True)
            return error, error
        result = self.build_result(tool_use_id, [image_block], False)
        traced = self.build_result(tool_use_id, [{"type": "text", "text": summary}], False)
        return result, traced

    def build_result(self, tool_use_id: str, content: list[dict[str, Any]], is_error: bool) -> dict[str, Any]:
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content, "is_error": is_error}


class CacheMarker:
    """Keeps a single prompt-cache breakpoint on the last content block of the conversation."""

    def mark_last_block(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            content = message["content"]
            if isinstance(content, list):
                for block in content:
                    block.pop("cache_control", None)
        last_content = messages[-1]["content"]
        if isinstance(last_content, list) and last_content:
            last_content[-1]["cache_control"] = {"type": "ephemeral"}


class UsageMeter:
    """Adds up token usage across turns and prices it."""

    def __init__(self, model: str):
        self.model = model
        self.totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0}

    def add_usage(self, usage: Any) -> None:
        for key in self.totals:
            self.totals[key] = self.totals[key] + int(getattr(usage, key, 0) or 0)

    def compute_cost_usd(self) -> float:
        input_price, output_price = PRICE_PER_M.get(self.model, (0.0, 0.0))
        cost = self.totals["input_tokens"] * input_price
        cost = cost + self.totals["cache_creation_input_tokens"] * input_price * CACHE_WRITE_RATE
        cost = cost + self.totals["cache_read_input_tokens"] * input_price * CACHE_READ_RATE
        cost = cost + self.totals["output_tokens"] * output_price
        return cost / 1_000_000


class TraceRecorder:
    """Records the conversation as stream-json style events so the existing trace tools can read it."""

    def __init__(self):
        self.events: list[dict[str, Any]] = []
        self.tool_counts: dict[str, int] = {}
        self.bash_commands: list[str] = []

    def record_assistant(self, content: list[dict[str, Any]]) -> None:
        self.events.append({"type": "assistant", "message": {"role": "assistant", "content": content}})
        for block in content:
            if block.get("type") != "tool_use":
                continue
            name = block.get("name", "")
            self.tool_counts[name] = self.tool_counts.get(name, 0) + 1
            if name == "bash":
                self.bash_commands.append(str((block.get("input") or {}).get("command", ""))[:2000])

    def record_tool_results(self, results: list[dict[str, Any]]) -> None:
        self.events.append({"type": "user", "message": {"role": "user", "content": results}})

    def record_result(self, meter: UsageMeter, turns: int, stop_reason: str | None) -> None:
        self.events.append({"type": "result", "subtype": "success", "is_error": False, "num_turns": turns,
                            "stop_reason": stop_reason, "total_cost_usd": meter.compute_cost_usd(),
                            "usage": dict(meter.totals)})


class AgentLoop:
    """Runs the tool-use conversation until the model stops calling tools or a limit is hit."""

    def __init__(self, client: Any, settings: dict[str, Any], workdir: Path):
        self.client = client
        self.settings = settings
        self.toolbox = AgentToolbox(workdir)
        self.cache_marker = CacheMarker()
        self.meter = UsageMeter(settings["model"])
        self.recorder = TraceRecorder()

    def run_conversation(self, prompt: str) -> None:
        messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        deadline = time.monotonic() + self.settings["timeout_s"]
        turns = 0
        stop_reason = None
        while turns < self.settings["max_turns"]:
            if time.monotonic() > deadline:
                raise ProviderTransientError("anthropic_agent_extract: wall-clock timeout")
            response = self.send_request(messages)
            turns = turns + 1
            self.meter.add_usage(response.usage)
            content = self.convert_content(response.content)
            self.recorder.record_assistant(content)
            messages.append({"role": "assistant", "content": content})
            stop_reason = response.stop_reason
            if stop_reason != "tool_use":
                break
            messages.append({"role": "user", "content": self.run_tool_calls(content)})
        self.recorder.record_result(self.meter, turns, stop_reason)

    def run_tool_calls(self, content: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        traced = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            result, traced_result = self.toolbox.run_tool(block)
            results.append(result)
            traced.append(traced_result)
        self.recorder.record_tool_results(traced)
        return results

    def send_request(self, messages: list[dict[str, Any]]) -> Any:
        self.cache_marker.mark_last_block(messages)
        arguments = {"model": self.settings["model"], "max_tokens": self.settings["max_tokens"],
                     "system": SYSTEM_PROMPT, "tools": AgentToolbox.TOOLS, "messages": messages}
        if self.settings["temperature"] is not None:
            arguments["temperature"] = self.settings["temperature"]
        attempt = 0
        while True:
            try:
                return self.client.messages.create(**arguments)
            except Exception as error:
                attempt = attempt + 1
                if attempt > MAX_API_RETRIES or not self.is_retryable(error):
                    raise self.classify_error(error) from error
                time.sleep(RETRY_WAIT_S * attempt)

    def convert_content(self, blocks: list[Any]) -> list[dict[str, Any]]:
        content = []
        for block in blocks:
            content.append(block.model_dump(exclude_none=True))
        return content

    def is_retryable(self, error: Exception) -> bool:
        status = getattr(error, "status_code", None)
        if status in (429, 500, 502, 503, 504, 529):
            return True
        return type(error).__name__ in ("APIConnectionError", "APITimeoutError")

    def classify_error(self, error: Exception) -> Exception:
        status = getattr(error, "status_code", None)
        if status == 429:
            return ProviderRateLimitError(f"anthropic_agent_extract rate-limited: {error}")
        if self.is_retryable(error):
            return ProviderTransientError(f"anthropic_agent_extract transient API error: {error}")
        return ProviderPermanentError(f"anthropic_agent_extract API error: {error}")


@register_provider("anthropic_agent_extract")
class AnthropicAgentExtractProvider(ClaudeCodeExtractProvider):
    """Extracts a document with our own API tool-use loop, reusing the Claude Code provider's prompt and outputs."""

    DEFAULT_MODEL = "claude-opus-4-6"

    def __init__(self, provider_name: str, base_config: dict[str, Any] | None = None):
        super().__init__(provider_name, base_config)
        self._temperature: float | None = self.base_config.get("temperature")
        self._max_turns: int = int(self.base_config.get("max_turns", 60))
        self._max_tokens: int = int(self.base_config.get("max_tokens", 16000))
        self._api_key = self.base_config.get("api_key") or os.getenv("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise ProviderConfigError("ANTHROPIC_API_KEY environment variable not set")

    def _config_snapshot(self) -> dict[str, Any]:
        snapshot = super()._config_snapshot()
        snapshot["harness"] = "anthropic_agent_extract"
        snapshot["temperature"] = self._temperature
        snapshot["max_turns"] = self._max_turns
        snapshot["max_tokens"] = self._max_tokens
        snapshot["system_prompt"] = SYSTEM_PROMPT
        return snapshot

    def build_settings(self) -> dict[str, Any]:
        return {"model": self._model, "temperature": self._temperature, "max_turns": self._max_turns,
                "max_tokens": self._max_tokens, "timeout_s": self._timeout_s}

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

    def run_agent(self, source: Path, schema: dict[str, Any]) -> dict[str, Any]:
        import anthropic

        client = anthropic.Anthropic(api_key=self._api_key)
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            staged_name = f"input{source.suffix.lower()}"
            shutil.copy2(source, workdir / staged_name)
            prompt = self._build_prompt(schema, staged_name)
            loop = AgentLoop(client, self.build_settings(), workdir)
            loop.run_conversation(prompt)
            outcome = {"prompt": prompt, "loop": loop, "data": self._read_output_json(workdir)}
            outcome["citations"] = None
            outcome["files"] = None
            if self._evidence_mode:
                outcome["citations"] = read_agent_citations_file(workdir / "citations.json")
            if self._is_full_trace:
                outcome["files"] = self._read_workdir_texts(workdir, staged_name)
        return outcome

    def build_raw_output(self, outcome: dict[str, Any], num_pages: int) -> dict[str, Any]:
        loop = outcome["loop"]
        cost_usd = loop.meter.compute_cost_usd()
        raw_output: dict[str, Any] = {
            "data": outcome["data"], "model": self._model, "usage": dict(loop.meter.totals), "num_pages": num_pages,
            "cost_usd": cost_usd, "cost_per_page_usd": cost_usd / num_pages if num_pages else None,
            "cost_exceeded_budget": self._max_cost_usd is not None and cost_usd > self._max_cost_usd,
            "_config": self._config_snapshot(), "tool_counts": loop.recorder.tool_counts,
            "bash_commands": loop.recorder.bash_commands, "_trace_tail": loop.recorder.events[-20:],
        }
        if self._is_full_trace:
            raw_output["_prompt"] = outcome["prompt"]
            raw_output["_trace_full"] = loop.recorder.events
            raw_output["_workdir_files"] = outcome["files"]
        if self._evidence_mode:
            citations, stats = citations_from_agent_file(outcome["citations"], source="anthropic_agent_evidence")
            raw_output["field_citations"] = [citation.model_dump() for citation in citations]
            raw_output["evidence_stats"] = stats.as_dict()
        return raw_output

    def run_inference(self, pipeline: PipelineSpec, request: InferenceRequest) -> RawInferenceResult:
        source = self.check_request(request)
        started_at = datetime.now()
        schema = self._prepare_schema(request.schema_override)
        outcome = self.run_agent(source, schema)
        completed_at = datetime.now()
        raw_output = self.build_raw_output(outcome, page_count(source))
        return RawInferenceResult(
            request=request, pipeline=pipeline, pipeline_name=pipeline.pipeline_name,
            product_type=request.product_type, raw_output=raw_output, started_at=started_at,
            completed_at=completed_at, latency_in_ms=int((completed_at - started_at).total_seconds() * 1000),
        )

    def cancel(self, example_id: str) -> bool:
        return False
