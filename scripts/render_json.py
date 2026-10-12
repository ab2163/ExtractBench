"""Render a JSON file as an HTML page with a CSS stylesheet, showing structure with indentation and color instead of brackets.

Usage:
    uv run python scripts/render_json.py data/short/budget_appendix_legis.test.json --output_dir traces/budget_appendix_legis_schema --path data_schema
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

HTML_NAME = "json.html"
CSS_NAME = "json.css"
DEPTH_COLOR_COUNT = 6


class ScalarFormatter:
    """Turns a JSON scalar into its verbatim text and a CSS class for its type."""

    def format_scalar(self, value: Any) -> tuple[str, str]:
        if value is None:
            return "null", "null"
        if isinstance(value, bool):
            return json.dumps(value), "boolean"
        if isinstance(value, (int, float)):
            return json.dumps(value), "number"
        return str(value), "string"


class NodeRenderer:
    """Renders any JSON value as nested HTML blocks, one indentation level per nesting level."""

    def __init__(self, formatter: ScalarFormatter):
        self.formatter = formatter

    def render_value(self, value: Any, depth: int) -> str:
        if isinstance(value, dict):
            return self.render_object(value, depth)
        if isinstance(value, list):
            return self.render_array(value, depth)
        return self.render_scalar(value)

    def render_object(self, value: dict, depth: int) -> str:
        if not value:
            return '<span class="empty">empty object</span>'
        parts = [f'<div class="block object {self.choose_depth_class(depth)}">']
        for key, child in value.items():
            label = f'<span class="key">{html.escape(str(key))}</span>'
            parts.append(self.render_entry(label, child, depth))
        parts.append("</div>")
        return "".join(parts)

    def render_array(self, value: list, depth: int) -> str:
        if not value:
            return '<span class="empty">empty list</span>'
        parts = [f'<div class="block array {self.choose_depth_class(depth)}">']
        for index, child in enumerate(value):
            label = f'<span class="index">{index}</span>'
            parts.append(self.render_entry(label, child, depth))
        parts.append("</div>")
        return "".join(parts)

    def render_entry(self, label: str, child: Any, depth: int) -> str:
        if self.is_container(child) and child:
            body = self.render_value(child, depth + 1)
            return f'<div class="entry nested">{label}{body}</div>'
        return f'<div class="entry inline">{label}{self.render_value(child, depth + 1)}</div>'

    def render_scalar(self, value: Any) -> str:
        text, css_class = self.formatter.format_scalar(value)
        return f'<span class="value {css_class}">{html.escape(text)}</span>'

    def is_container(self, value: Any) -> bool:
        return isinstance(value, (dict, list))

    def choose_depth_class(self, depth: int) -> str:
        return f"depth-{depth % DEPTH_COLOR_COUNT}"


class PageBuilder:
    """Wraps the rendered JSON tree in an HTML page with a title and a legend."""

    LEGEND = (
        '<div class="legend">'
        '<span class="key">key</span>'
        '<span class="index">0</span> list position'
        '<span class="value string">string</span>'
        '<span class="value number">number</span>'
        '<span class="value boolean">true / false</span>'
        '<span class="value null">null</span>'
        "</div>"
    )

    def build_page(self, title: str, tree_html: str) -> str:
        escaped_title = html.escape(title)
        return "\n".join([
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{escaped_title}</title>",
            f'<link rel="stylesheet" href="{CSS_NAME}">',
            "</head>",
            "<body>",
            f"<h1>{escaped_title}</h1>",
            self.LEGEND,
            f'<main class="tree">{tree_html}</main>',
            "</body>",
            "</html>",
        ])


class StylesheetWriter:
    """Holds the JSON page's CSS: one rail color per nesting depth and one text color per value type."""

    STYLESHEET = """:root {
  --page: #fcfcfb; --ink: #1d2129; --muted: #6a707c; --rule: #e3e4e8;
  --key: #1d2129; --string: #1b7a43; --number: #1f5fd1; --boolean: #a3329e; --null: #8a8f99;
  --depth-0: #1f5fd1; --depth-1: #1b7a43; --depth-2: #c27c0e; --depth-3: #a3329e; --depth-4: #0f8a8a; --depth-5: #c62828;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #15171c; --ink: #e8e9ed; --muted: #9a9fab; --rule: #2d313a;
    --key: #e8e9ed; --string: #5fd08f; --number: #7ea3ff; --boolean: #d36bd0; --null: #8a8f99;
    --depth-0: #7ea3ff; --depth-1: #5fd08f; --depth-2: #e0a23a; --depth-3: #d36bd0; --depth-4: #3cc4c4; --depth-5: #ff6b6b;
  }
}
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1100px; padding: 24px 16px; background: var(--page); color: var(--ink);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
h1 { font-size: 18px; margin: 0 0 8px; overflow-wrap: anywhere; }
.legend { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 14px; color: var(--muted);
  font-size: 13px; margin: 0 0 20px; padding-bottom: 12px; border-bottom: 1px solid var(--rule); }
.block { border-left: 3px solid var(--rail); padding-left: 12px; margin: 2px 0 6px 2px; }
.depth-0 { --rail: var(--depth-0); } .depth-1 { --rail: var(--depth-1); } .depth-2 { --rail: var(--depth-2); }
.depth-3 { --rail: var(--depth-3); } .depth-4 { --rail: var(--depth-4); } .depth-5 { --rail: var(--depth-5); }
.entry { padding: 1px 0; }
.entry.inline { display: flex; gap: 12px; align-items: baseline; }
.entry.nested > .key, .entry.nested > .index { display: block; }
.key { font-weight: 600; color: var(--key); flex: 0 0 auto; }
.index { font: 12px ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--muted);
  border: 1px solid var(--rule); border-radius: 4px; padding: 0 5px; flex: 0 0 auto; }
.value { white-space: pre-wrap; overflow-wrap: anywhere; min-width: 0; }
.string { color: var(--string); }
.number, .boolean, .null { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.number { color: var(--number); }
.boolean { color: var(--boolean); }
.null, .empty { color: var(--null); font-style: italic; }
"""

    def write_stylesheet(self, path: Path) -> None:
        path.write_text(self.STYLESHEET, encoding="utf-8")


class JsonPageRenderer:
    """Loads a JSON file, optionally selects a sub-tree, and writes json.html and json.css."""

    def __init__(self):
        self.node_renderer = NodeRenderer(ScalarFormatter())
        self.page_builder = PageBuilder()
        self.stylesheet_writer = StylesheetWriter()

    def render_file(self, json_path: Path, output_dir: Path, key_path: str | None) -> Path:
        with open(json_path, encoding="utf-8") as handle:
            document = json.load(handle)
        title = json_path.name
        if key_path:
            document = self.select_subtree(document, key_path)
            title = f"{json_path.name} › {key_path}"
        tree_html = self.node_renderer.render_value(document, 0)
        output_dir.mkdir(parents=True, exist_ok=True)
        html_path = output_dir / HTML_NAME
        html_path.write_text(self.page_builder.build_page(title, tree_html), encoding="utf-8")
        self.stylesheet_writer.write_stylesheet(output_dir / CSS_NAME)
        return html_path

    def select_subtree(self, document: Any, key_path: str) -> Any:
        current = document
        for segment in key_path.split("."):
            if isinstance(current, list):
                current = current[int(segment)]
            else:
                current = current[segment]
        return current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("json_file", type=Path, help="JSON file to render")
    parser.add_argument("--output_dir", type=Path, required=True, help="folder for json.html and json.css")
    parser.add_argument("--path", default=None, help="dot-separated keys selecting part of the file, e.g. data_schema")
    arguments = parser.parse_args()
    html_path = JsonPageRenderer().render_file(arguments.json_file, arguments.output_dir, arguments.path)
    print(f"Wrote {html_path} and {arguments.output_dir / CSS_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
