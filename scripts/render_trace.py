"""Render a full Claude Code agent trace from a raw.json file as an HTML page with a CSS stylesheet.

Usage:
    uv run python scripts/render_trace.py output_pilot_trace/<pipeline>/short/budget_appendix_legis.raw.json --output_dir traces/budget_appendix_legis
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass
from pathlib import Path

HTML_NAME = "trace.html"
CSS_NAME = "trace.css"
NOT_AVAILABLE = "N/A"


@dataclass
class TraceEvent:
    """One entry in the rendered trace: its type, a CSS kind and its content."""

    event_type: str
    kind: str
    content: str


class BlockFormatter:
    """Turns one content block of an assistant or user message into a TraceEvent."""

    def format_block(self, role: str, block: dict) -> TraceEvent:
        block_type = block.get("type", "unknown")
        if block_type == "text":
            return TraceEvent(f"{role} · text", "text", block.get("text", ""))
        if block_type == "thinking":
            return TraceEvent(f"{role} · thinking", "thinking", NOT_AVAILABLE)
        if block_type == "tool_use":
            return self.format_tool_use(role, block)
        if block_type == "tool_result":
            return self.format_tool_result(role, block)
        return TraceEvent(f"{role} · {block_type}", "other", json.dumps(block, indent=2))

    def format_tool_use(self, role: str, block: dict) -> TraceEvent:
        tool_input = block.get("input") or {}
        content = tool_input.get("command")
        if content is None:
            content = json.dumps(tool_input, indent=2)
        event_type = f"{role} · tool_use · {block.get('name', '')}"
        return TraceEvent(event_type, "tool-use", content)

    def format_tool_result(self, role: str, block: dict) -> TraceEvent:
        content = block.get("content")
        if isinstance(content, list):
            content = self.join_text_parts(content)
        event_type = f"{role} · tool_result"
        kind = "tool-result"
        if block.get("is_error"):
            event_type = event_type + " · error"
            kind = "tool-error"
        return TraceEvent(event_type, kind, str(content))

    def join_text_parts(self, parts: list) -> str:
        texts = []
        for part in parts:
            if isinstance(part, dict):
                texts.append(part.get("text", ""))
        return "\n".join(texts)


class EventExtractor:
    """Converts the saved stream-json events into a flat, ordered list of TraceEvents."""

    def __init__(self, formatter: BlockFormatter):
        self.formatter = formatter

    def extract_events(self, raw_events: list) -> list[TraceEvent]:
        events = []
        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                continue
            events.extend(self.extract_from_event(raw_event))
        return events

    def extract_from_event(self, raw_event: dict) -> list[TraceEvent]:
        event_type = raw_event.get("type", "unknown")
        if event_type in ("assistant", "user"):
            return self.extract_message_blocks(event_type, raw_event)
        subtype = raw_event.get("subtype")
        label = event_type
        if subtype:
            label = f"{event_type} · {subtype}"
        kind = self.choose_kind(event_type, subtype)
        return [TraceEvent(label, kind, json.dumps(raw_event, indent=2))]

    def extract_message_blocks(self, role: str, raw_event: dict) -> list[TraceEvent]:
        message = raw_event.get("message") or {}
        blocks = message.get("content") or []
        if isinstance(blocks, str):
            return [TraceEvent(f"{role} · text", "text", blocks)]
        events = []
        for block in blocks:
            if isinstance(block, dict):
                events.append(self.formatter.format_block(role, block))
        return events

    def choose_kind(self, event_type: str, subtype: str | None) -> str:
        if event_type == "result":
            return "result"
        if subtype == "thinking_tokens":
            return "thinking-tokens"
        if event_type == "system":
            return "system"
        if event_type == "rate_limit_event":
            return "rate-limit"
        return "other"


class HtmlRenderer:
    """Builds the trace page as a top-to-bottom list of events."""

    def render_page(self, title: str, events: list[TraceEvent]) -> str:
        parts = [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{html.escape(title)}</title>",
            f'<link rel="stylesheet" href="{CSS_NAME}">',
            "</head>",
            "<body>",
            f"<h1>{html.escape(title)}</h1>",
            f'<p class="count">{len(events)} events</p>',
            '<ol class="trace">',
        ]
        for index, event in enumerate(events, start=1):
            parts.append(self.render_event(index, event))
        parts.extend(["</ol>", "</body>", "</html>"])
        return "\n".join(parts)

    def render_event(self, index: int, event: TraceEvent) -> str:
        return (
            f'<li class="event kind-{event.kind}">'
            f'<div class="event-type"><span class="index">{index}</span>{html.escape(event.event_type)}</div>'
            f'<pre class="content">{html.escape(event.content)}</pre>'
            "</li>"
        )


class StylesheetWriter:
    """Holds the trace page's CSS, with one color per event kind."""

    STYLESHEET = """:root {
  --page: #ffffff; --ink: #1d1d1f; --muted: #6e6e73; --panel: #f6f6f8; --rule: #e2e2e6;
  --prompt: #6b4fd8; --system: #8a8a8e; --rate-limit: #b0b0b4; --thinking: #c27c0e;
  --thinking-tokens: #d9a441; --text: #1f7a3d; --tool-use: #1f5fd1; --tool-result: #0f8a8a;
  --tool-error: #c62828; --result: #a3329e; --other: #555555;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #141416; --ink: #ececf0; --muted: #9a9aa0; --panel: #1e1e22; --rule: #2e2e34;
    --prompt: #9d86ff; --system: #9a9aa0; --rate-limit: #6e6e73; --thinking: #e0a23a;
    --thinking-tokens: #c9a24f; --text: #4cc472; --tool-use: #5b93ff; --tool-result: #3cc4c4;
    --tool-error: #ff6b6b; --result: #d36bd0; --other: #9a9aa0;
  }
}
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1100px; padding: 24px 16px; background: var(--page); color: var(--ink);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; overflow-wrap: anywhere; }
.count { color: var(--muted); margin: 0 0 20px; }
.trace { list-style: none; margin: 0; padding: 0; }
.event { --accent: var(--other); border-left: 5px solid var(--accent); background: var(--panel);
  border-radius: 6px; margin: 0 0 12px; overflow: hidden; }
.event-type { font-weight: 600; color: var(--accent); padding: 8px 12px; border-bottom: 1px solid var(--rule); }
.index { display: inline-block; min-width: 2.5em; color: var(--muted); font-weight: 400; }
.content { margin: 0; padding: 10px 12px; max-height: 32em; overflow: auto; white-space: pre-wrap;
  overflow-wrap: anywhere; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
.kind-prompt { --accent: var(--prompt); }
.kind-system { --accent: var(--system); }
.kind-rate-limit { --accent: var(--rate-limit); }
.kind-thinking { --accent: var(--thinking); }
.kind-thinking-tokens { --accent: var(--thinking-tokens); }
.kind-text { --accent: var(--text); }
.kind-tool-use { --accent: var(--tool-use); }
.kind-tool-result { --accent: var(--tool-result); }
.kind-tool-error { --accent: var(--tool-error); }
.kind-result { --accent: var(--result); }
"""

    def write_stylesheet(self, path: Path) -> None:
        path.write_text(self.STYLESHEET, encoding="utf-8")


class TraceRenderer:
    """Loads a raw.json trace and writes trace.html and trace.css into an output folder."""

    def __init__(self):
        self.extractor = EventExtractor(BlockFormatter())
        self.html_renderer = HtmlRenderer()
        self.stylesheet_writer = StylesheetWriter()

    def render_trace(self, raw_json_path: Path, output_dir: Path) -> Path:
        raw_output = self.load_raw_output(raw_json_path)
        raw_events = raw_output.get("_trace_full")
        if not raw_events:
            raise ValueError(f"{raw_json_path} has no _trace_full; re-run with a fulltrace pipeline")
        events = []
        prompt = raw_output.get("_prompt")
        if prompt:
            events.append(TraceEvent("prompt", "prompt", prompt))
        events.extend(self.extractor.extract_events(raw_events))
        output_dir.mkdir(parents=True, exist_ok=True)
        title = f"Agent trace: {raw_json_path.name.removesuffix('.raw.json')}"
        page = self.html_renderer.render_page(title, events)
        html_path = output_dir / HTML_NAME
        html_path.write_text(page, encoding="utf-8")
        self.stylesheet_writer.write_stylesheet(output_dir / CSS_NAME)
        return html_path

    def load_raw_output(self, raw_json_path: Path) -> dict:
        with open(raw_json_path, encoding="utf-8") as handle:
            document = json.load(handle)
        return document.get("raw_output") or document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("raw_json", type=Path, help="raw.json written by a fulltrace pipeline")
    parser.add_argument("--output_dir", type=Path, required=True, help="folder for trace.html and trace.css")
    arguments = parser.parse_args()
    html_path = TraceRenderer().render_trace(arguments.raw_json, arguments.output_dir)
    print(f"Wrote {html_path} and {arguments.output_dir / CSS_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
