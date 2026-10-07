from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CORRECTIONS_PATH = Path(__file__).with_name("answer_key_corrections.json")
TEST_JSON_SUFFIX = ".test.json"


class ExpectedValueWriter:
    """Writes a value into a nested expected-output dict at a dotted path."""

    def write_value(self, expected: dict[str, Any], dotted_path: str, value: Any) -> None:
        parts = dotted_path.split(".")
        node = expected
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value


class FieldRuleEditor:
    """Drops and replaces entries in a test case's _field_rules dict."""

    def is_dropped(self, field_path: str, prefixes: list[str]) -> bool:
        for prefix in prefixes:
            if field_path.startswith(prefix):
                return True
        return False

    def drop_rules_by_prefix(self, rules: dict[str, Any], prefixes: list[str]) -> int:
        dropped_paths = []
        for field_path in rules:
            if self.is_dropped(field_path, prefixes):
                dropped_paths.append(field_path)
        for field_path in dropped_paths:
            del rules[field_path]
        return len(dropped_paths)

    def replace_rules(self, rules: dict[str, Any], replacements: dict[str, Any]) -> None:
        for field_path, rule in replacements.items():
            rules[field_path] = rule


class AnswerKeyCorrector:
    """Applies committed answer-key corrections to a downloaded test.json payload."""

    def __init__(self, corrections_path: Path = CORRECTIONS_PATH):
        self.corrections = self.load_corrections(corrections_path)
        self.value_writer = ExpectedValueWriter()
        self.rule_editor = FieldRuleEditor()

    def load_corrections(self, corrections_path: Path) -> dict[str, Any]:
        if not corrections_path.exists():
            return {}
        return json.loads(corrections_path.read_text(encoding="utf-8"))

    def build_case_key(self, test_json_path: Path) -> str:
        name = test_json_path.name
        if name.endswith(TEST_JSON_SUFFIX):
            name = name[: -len(TEST_JSON_SUFFIX)]
        return f"{test_json_path.parent.name}/{name}"

    def apply_corrections(self, test_json_path: Path, test_config: dict[str, Any]) -> bool:
        case_key = self.build_case_key(Path(test_json_path))
        correction = self.corrections.get(case_key)
        if correction is None:
            return False
        self.apply_expected_values(test_config, correction.get("set_expected", {}))
        self.apply_rule_changes(test_config, correction)
        logger.info("Applied answer-key correction to %s: %s", case_key, correction.get("reason", ""))
        return True

    def apply_expected_values(self, test_config: dict[str, Any], values: dict[str, Any]) -> None:
        expected = test_config.get("expected_output")
        if not isinstance(expected, dict):
            return
        for dotted_path, value in values.items():
            self.value_writer.write_value(expected, dotted_path, value)

    def apply_rule_changes(self, test_config: dict[str, Any], correction: dict[str, Any]) -> None:
        rules = test_config.get("_field_rules")
        if not isinstance(rules, dict):
            return
        self.rule_editor.drop_rules_by_prefix(rules, correction.get("drop_rule_prefixes", []))
        self.rule_editor.replace_rules(rules, correction.get("set_rules", {}))


ANSWER_KEY_CORRECTOR = AnswerKeyCorrector()
