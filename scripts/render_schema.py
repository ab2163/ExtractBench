"""Render a JSON Schema as an HTML page with a CSS stylesheet: one card per entity, listing its attributes and how entities link.

Usage:
    uv run python scripts/render_schema.py data/short/budget_appendix_legis.test.json --output_dir traces/budget_appendix_legis_schema --path data_schema
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HTML_NAME = "schema.html"
CSS_NAME = "schema.css"
ROOT_NAME = "Output"
ENTITY_COLOR_COUNT = 6
DEFS_KEYS = ("$defs", "definitions")
ENTITY_HANDLED_KEYS = frozenset({"type", "description", "properties", "required", "additionalProperties", "$defs", "definitions"})
ATTRIBUTE_HANDLED_KEYS = frozenset({"type", "description", "items", "$ref", "anyOf", "oneOf"})


@dataclass
class SchemaEntity:
    """One object type in the schema: its name, its schema and its position for coloring."""

    name: str
    schema: dict
    color_index: int
    referenced_by: list[str] = field(default_factory=list)

    def build_anchor(self) -> str:
        return "entity-" + self.name.replace(".", "-").replace(" ", "-")


class EntityCollector:
    """Finds every entity in a schema: the root, each definition, and each inline object."""

    def __init__(self):
        self.entities: list[SchemaEntity] = []
        self.by_schema_id: dict[int, SchemaEntity] = {}
        self.by_name: dict[str, SchemaEntity] = {}

    def collect_entities(self, root: dict) -> list[SchemaEntity]:
        self.add_entity(root.get("title") or ROOT_NAME, root)
        for defs_key in DEFS_KEYS:
            definitions = root.get(defs_key) or {}
            for name, definition in definitions.items():
                self.add_entity(name, definition)
        index = 0
        while index < len(self.entities):
            self.collect_inline_entities(self.entities[index])
            index += 1
        return self.entities

    def add_entity(self, name: str, schema: dict) -> SchemaEntity:
        entity = SchemaEntity(name, schema, len(self.entities) % ENTITY_COLOR_COUNT)
        self.entities.append(entity)
        self.by_schema_id[id(schema)] = entity
        self.by_name[name] = entity
        return entity

    def collect_inline_entities(self, entity: SchemaEntity) -> None:
        properties = entity.schema.get("properties") or {}
        for attribute_name, attribute_schema in properties.items():
            target = self.find_object_schema(attribute_schema)
            if target is not None and id(target) not in self.by_schema_id:
                self.add_entity(f"{entity.name}.{attribute_name}", target)

    def find_object_schema(self, schema: Any) -> dict | None:
        if not isinstance(schema, dict):
            return None
        if "properties" in schema:
            return schema
        if schema.get("type") == "array":
            return self.find_object_schema(schema.get("items"))
        return None

    def find_by_ref(self, ref: str) -> SchemaEntity | None:
        return self.by_name.get(ref.rsplit("/", 1)[-1])


class TypeDescriber:
    """Describes an attribute's type in words, linking to entity cards where the type is an entity."""

    def __init__(self, collector: EntityCollector):
        self.collector = collector

    def describe_type(self, schema: Any) -> str:
        if not isinstance(schema, dict):
            return self.render_word("any")
        if "$ref" in schema:
            return self.render_ref(schema["$ref"])
        if id(schema) in self.collector.by_schema_id:
            return self.render_entity_chip(self.collector.by_schema_id[id(schema)])
        for union_key in ("anyOf", "oneOf"):
            if union_key in schema:
                return self.describe_union(schema[union_key])
        type_value = schema.get("type")
        if type_value == "array":
            return self.render_word("list of") + " " + self.describe_type(schema.get("items"))
        if isinstance(type_value, list):
            return self.describe_type_list(type_value)
        return self.render_word(str(type_value or "any"))

    def describe_union(self, options: list) -> str:
        parts = []
        for option in options:
            parts.append(self.describe_type(option))
        return ' <span class="or">or</span> '.join(parts)

    def describe_type_list(self, type_names: list) -> str:
        parts = []
        for type_name in type_names:
            parts.append(self.render_word(str(type_name)))
        return ' <span class="or">or</span> '.join(parts)

    def render_ref(self, ref: str) -> str:
        entity = self.collector.find_by_ref(ref)
        if entity is None:
            return self.render_word(ref)
        return self.render_entity_chip(entity)

    def render_entity_chip(self, entity: SchemaEntity) -> str:
        return (f'<a class="entity-chip color-{entity.color_index}" href="#{entity.build_anchor()}">'
                f"{html.escape(entity.name)}</a>")

    def render_word(self, word: str) -> str:
        return f'<span class="type-word type-{html.escape(word.split()[0])}">{html.escape(word)}</span>'


class RuleLister:
    """Lists schema keywords the card layout does not otherwise show, so nothing in the schema is hidden."""

    def list_rules(self, schema: dict, handled_keys: frozenset, prefix: str) -> list[str]:
        rules = []
        for key, value in schema.items():
            if key in handled_keys:
                continue
            rules.append(self.format_rule(prefix + key, value))
        items = schema.get("items")
        if isinstance(items, dict) and "properties" not in items:
            rules.extend(self.list_rules(items, ATTRIBUTE_HANDLED_KEYS, prefix + "items."))
        return rules

    def format_rule(self, key: str, value: Any) -> str:
        return (f'<span class="rule"><span class="rule-key">{html.escape(key)}</span> '
                f'<span class="rule-value">{html.escape(json.dumps(value, ensure_ascii=False))}</span></span>')


class AttributeTableRenderer:
    """Renders an entity's attributes as a table of name, type, required flag and description."""

    def __init__(self, describer: TypeDescriber, rule_lister: RuleLister):
        self.describer = describer
        self.rule_lister = rule_lister

    def render_table(self, entity: SchemaEntity) -> str:
        properties = entity.schema.get("properties") or {}
        if not properties:
            return '<p class="none">No attributes.</p>'
        required = entity.schema.get("required") or []
        parts = ['<table class="attributes"><thead><tr>',
                 "<th>Attribute</th><th>Type</th><th>Description</th>",
                 "</tr></thead><tbody>"]
        for attribute_name, attribute_schema in properties.items():
            is_required = attribute_name in required
            parts.append(self.render_row(attribute_name, attribute_schema, is_required))
        parts.append("</tbody></table>")
        return "".join(parts)

    def render_row(self, attribute_name: str, schema: Any, is_required: bool) -> str:
        if not isinstance(schema, dict):
            schema = {}
        flag = '<span class="flag required">required</span>' if is_required else '<span class="flag optional">optional</span>'
        description = html.escape(schema.get("description", ""))
        rules = "".join(self.rule_lister.list_rules(schema, ATTRIBUTE_HANDLED_KEYS, ""))
        return ("<tr>"
                f'<td class="attr-name"><span class="name">{html.escape(attribute_name)}</span>{flag}</td>'
                f'<td class="attr-type">{self.describer.describe_type(schema)}</td>'
                f'<td class="attr-desc">{description}<div class="rules">{rules}</div></td>'
                "</tr>")


class EntityCardRenderer:
    """Renders one entity as a card: colored name bar, description, links, attribute table and extra rules."""

    def __init__(self, table_renderer: AttributeTableRenderer, rule_lister: RuleLister, describer: TypeDescriber):
        self.table_renderer = table_renderer
        self.rule_lister = rule_lister
        self.describer = describer

    def render_card(self, entity: SchemaEntity) -> str:
        description = entity.schema.get("description", "")
        rules = "".join(self.rule_lister.list_rules(entity.schema, ENTITY_HANDLED_KEYS, ""))
        return (f'<section class="card color-{entity.color_index}" id="{entity.build_anchor()}">'
                f'<header class="card-head"><span class="kind">entity</span>'
                f'<h2>{html.escape(entity.name)}</h2>{self.render_closed_flag(entity)}</header>'
                f'<div class="card-body">{self.render_used_by(entity)}'
                f'<p class="description">{html.escape(description)}</p>'
                f"{self.table_renderer.render_table(entity)}"
                f'<div class="rules">{rules}</div></div></section>')

    def render_closed_flag(self, entity: SchemaEntity) -> str:
        if entity.schema.get("additionalProperties") is False:
            return '<span class="closed">no other attributes allowed</span>'
        return ""

    def render_used_by(self, entity: SchemaEntity) -> str:
        if not entity.referenced_by:
            return ""
        return '<p class="used-by">Used by ' + ", ".join(entity.referenced_by) + "</p>"


class RelationshipMapper:
    """Finds which attribute of which entity points to each other entity, for the overview and 'used by' lines."""

    def __init__(self, collector: EntityCollector, describer: TypeDescriber):
        self.collector = collector
        self.describer = describer

    def map_relationships(self, entities: list[SchemaEntity]) -> list[str]:
        lines = []
        for entity in entities:
            properties = entity.schema.get("properties") or {}
            for attribute_name, attribute_schema in properties.items():
                target = self.find_target(attribute_schema)
                if target is None:
                    continue
                is_list = isinstance(attribute_schema, dict) and attribute_schema.get("type") == "array"
                lines.append(self.render_line(entity, attribute_name, target, is_list))
                target.referenced_by.append(f"{self.describer.render_entity_chip(entity)} › {html.escape(attribute_name)}")
        return lines

    def find_target(self, schema: Any) -> SchemaEntity | None:
        if not isinstance(schema, dict):
            return None
        if "$ref" in schema:
            return self.collector.find_by_ref(schema["$ref"])
        if id(schema) in self.collector.by_schema_id:
            return self.collector.by_schema_id[id(schema)]
        if schema.get("type") == "array":
            return self.find_target(schema.get("items"))
        return None

    def render_line(self, source: SchemaEntity, attribute_name: str, target: SchemaEntity, is_list: bool) -> str:
        count = "has a list of" if is_list else "has one"
        return ('<li class="relation">'
                f"{self.describer.render_entity_chip(source)}"
                f'<span class="arrow">— <span class="name">{html.escape(attribute_name)}</span> · {count} →</span>'
                f"{self.describer.render_entity_chip(target)}</li>")


class SchemaPageRenderer:
    """Loads a schema, collects its entities and writes schema.html and schema.css."""

    def render_file(self, json_path: Path, output_dir: Path, key_path: str | None) -> Path:
        schema = self.load_schema(json_path, key_path)
        collector = EntityCollector()
        entities = collector.collect_entities(schema)
        describer = TypeDescriber(collector)
        rule_lister = RuleLister()
        relations = RelationshipMapper(collector, describer).map_relationships(entities)
        card_renderer = EntityCardRenderer(AttributeTableRenderer(describer, rule_lister), rule_lister, describer)
        cards = []
        for entity in entities:
            cards.append(card_renderer.render_card(entity))
        title = json_path.name if not key_path else f"{json_path.name} › {key_path}"
        page = PageBuilder().build_page(title, relations, cards)
        output_dir.mkdir(parents=True, exist_ok=True)
        html_path = output_dir / HTML_NAME
        html_path.write_text(page, encoding="utf-8")
        (output_dir / CSS_NAME).write_text(StylesheetWriter.STYLESHEET, encoding="utf-8")
        return html_path

    def load_schema(self, json_path: Path, key_path: str | None) -> dict:
        with open(json_path, encoding="utf-8") as handle:
            document = json.load(handle)
        if key_path:
            for segment in key_path.split("."):
                document = document[int(segment)] if isinstance(document, list) else document[segment]
        return document


class PageBuilder:
    """Assembles the page: title, relationship overview, then one card per entity."""

    def build_page(self, title: str, relations: list[str], cards: list[str]) -> str:
        escaped_title = html.escape(title)
        overview = ""
        if relations:
            overview = '<section class="overview"><h2>How the entities connect</h2><ul>' + "".join(relations) + "</ul></section>"
        return "\n".join([
            "<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{escaped_title}</title>", f'<link rel="stylesheet" href="{CSS_NAME}">',
            "</head>", "<body>", f"<h1>{escaped_title}</h1>", overview,
            '<main class="cards">', "\n".join(cards), "</main>", "</body>", "</html>",
        ])


class StylesheetWriter:
    """Holds the schema page's CSS: one color per entity, used on its card and on every link to it."""

    STYLESHEET = """:root {
  --page: #f7f7f5; --card: #ffffff; --ink: #1d2129; --muted: #646a76; --rule: #e2e3e7; --soft: #f1f2f4;
  --entity-0: #1f5fd1; --entity-1: #1b7a43; --entity-2: #b86e00; --entity-3: #a3329e; --entity-4: #0f8a8a; --entity-5: #c62828;
  --string: #1b7a43; --number: #1f5fd1; --boolean: #a3329e; --null: #8a8f99;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #131519; --card: #1b1e24; --ink: #e8e9ed; --muted: #9a9fab; --rule: #2d313a; --soft: #23272f;
    --entity-0: #7ea3ff; --entity-1: #5fd08f; --entity-2: #e0a23a; --entity-3: #d36bd0; --entity-4: #3cc4c4; --entity-5: #ff6b6b;
    --string: #5fd08f; --number: #7ea3ff; --boolean: #d36bd0; --null: #8a8f99;
  }
}
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1100px; padding: 24px 16px 48px; background: var(--page); color: var(--ink);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
h1 { font-size: 18px; margin: 0 0 16px; overflow-wrap: anywhere; }
h2 { margin: 0; }
.color-0 { --accent: var(--entity-0); } .color-1 { --accent: var(--entity-1); } .color-2 { --accent: var(--entity-2); }
.color-3 { --accent: var(--entity-3); } .color-4 { --accent: var(--entity-4); } .color-5 { --accent: var(--entity-5); }
.overview { background: var(--card); border: 1px solid var(--rule); border-radius: 10px; padding: 14px 16px; margin: 0 0 20px; }
.overview h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-bottom: 8px; }
.overview ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
.relation { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.arrow { color: var(--muted); }
.entity-chip { display: inline-block; font-weight: 600; color: var(--accent); text-decoration: none;
  border: 1.5px solid var(--accent); border-radius: 6px; padding: 0 8px; background: var(--card); }
.entity-chip:hover, .entity-chip:focus-visible { background: var(--accent); color: var(--card); }
.cards { display: grid; gap: 20px; }
.card { background: var(--card); border: 1.5px solid var(--accent); border-radius: 10px; overflow: hidden; scroll-margin-top: 16px; }
.card:target { box-shadow: 0 0 0 4px var(--accent); }
.card-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 12px; background: var(--accent); color: var(--card); padding: 10px 16px; }
.card-head h2 { font-size: 18px; }
.kind { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; opacity: 0.85; }
.closed { margin-left: auto; font-size: 12px; opacity: 0.9; }
.card-body { padding: 12px 16px 16px; display: grid; gap: 10px; min-width: 0; overflow-x: auto; }
.description { margin: 0; max-width: 80ch; }
.used-by { margin: 0; font-size: 13px; color: var(--muted); }
.attributes { width: 100%; border-collapse: collapse; }
.attributes th { text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted);
  border-bottom: 1.5px solid var(--rule); padding: 6px 10px 6px 0; }
.attributes td { vertical-align: top; border-bottom: 1px solid var(--rule); padding: 10px 10px 10px 0; }
.attr-name { width: 1%; white-space: nowrap; }
.attr-name .name { display: block; font-weight: 600; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.attr-type { width: 1%; white-space: nowrap; }
.attr-desc { color: var(--ink); min-width: 280px; }
.flag { display: inline-block; font-size: 11px; margin-top: 2px; padding: 0 6px; border-radius: 4px; }
.flag.required { background: var(--soft); color: var(--ink); font-weight: 600; }
.flag.optional { color: var(--muted); border: 1px dashed var(--rule); }
.type-word { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; color: var(--muted); }
.type-string { color: var(--string); } .type-number, .type-integer { color: var(--number); }
.type-boolean { color: var(--boolean); } .type-null { color: var(--null); font-style: italic; }
.or { color: var(--muted); font-size: 12px; }
.rules { display: flex; flex-wrap: wrap; gap: 6px; }
.rules:empty { display: none; }
.rule { font-size: 12px; background: var(--soft); border-radius: 4px; padding: 1px 6px; }
.rule-key { color: var(--muted); }
.rule-value { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }
.none { color: var(--muted); margin: 0; }
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("json_file", type=Path, help="JSON file holding the schema")
    parser.add_argument("--output_dir", type=Path, required=True, help="folder for schema.html and schema.css")
    parser.add_argument("--path", default=None, help="dot-separated keys to the schema inside the file, e.g. data_schema")
    arguments = parser.parse_args()
    html_path = SchemaPageRenderer().render_file(arguments.json_file, arguments.output_dir, arguments.path)
    print(f"Wrote {html_path} and {arguments.output_dir / CSS_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
