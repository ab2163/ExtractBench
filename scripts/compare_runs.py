"""Measure how often two agent runs on the same document agree, value by value and box by box.

Two values agree when their JSON strings are identical. Two boxes agree when they are on the same page with IoU > 0.5.

Usage:
    uv run python scripts/compare_runs.py <first>.raw.json <second>.raw.json [--is_listing]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

IOU_THRESHOLD = 0.5


class RunLoader:
    """Reads the extracted data and citations from a raw.json file."""

    def load_run(self, path: Path) -> dict[str, Any]:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
        raw_output = document.get("raw_output") or document
        return {"data": raw_output.get("data") or {}, "citations": raw_output.get("field_citations") or []}


class LeafFlattener:
    """Turns nested JSON into a map from dotted field path to leaf value."""

    def flatten_leaves(self, value: Any) -> dict[str, Any]:
        leaves: dict[str, Any] = {}
        self.add_leaves(value, "", leaves)
        return leaves

    def add_leaves(self, value: Any, path: str, leaves: dict[str, Any]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                self.add_leaves(child, self.join_key(path, key), leaves)
            return
        if isinstance(value, list) and value:
            for index, child in enumerate(value):
                self.add_leaves(child, f"{path}[{index}]", leaves)
            return
        leaves[path] = value

    def join_key(self, path: str, key: str) -> str:
        if path:
            return f"{path}.{key}"
        return key


class IouCalculator:
    """Computes intersection over union for two [x, y, width, height] boxes."""

    def compute_iou(self, first: list[float], second: list[float]) -> float:
        overlap_width = min(first[0] + first[2], second[0] + second[2]) - max(first[0], second[0])
        overlap_height = min(first[1] + first[3], second[1] + second[3]) - max(first[1], second[1])
        if overlap_width <= 0 or overlap_height <= 0:
            return 0.0
        overlap = overlap_width * overlap_height
        union = first[2] * first[3] + second[2] * second[3] - overlap
        if union <= 0:
            return 0.0
        return overlap / union


class ValueComparer:
    """Counts leaf values that are identical as JSON strings in both runs."""

    def __init__(self, flattener: LeafFlattener):
        self.flattener = flattener

    def compare_values(self, first_data: Any, second_data: Any) -> dict[str, Any]:
        first_leaves = self.flattener.flatten_leaves(first_data)
        second_leaves = self.flattener.flatten_leaves(second_data)
        result = {"all_paths": 0, "all_same": 0, "filled_paths": 0, "filled_same": 0, "differences": []}
        for path in sorted(set(first_leaves) | set(second_leaves)):
            first_text = json.dumps(first_leaves.get(path), ensure_ascii=False)
            second_text = json.dumps(second_leaves.get(path), ensure_ascii=False)
            is_same = first_text == second_text
            self.count_path(result, is_same, first_leaves.get(path) is None and second_leaves.get(path) is None)
            if not is_same:
                result["differences"].append((path, first_text, second_text))
        return result

    def count_path(self, result: dict[str, Any], is_same: bool, is_both_null: bool) -> None:
        result["all_paths"] = result["all_paths"] + 1
        if is_same:
            result["all_same"] = result["all_same"] + 1
        if is_both_null:
            return
        result["filled_paths"] = result["filled_paths"] + 1
        if is_same:
            result["filled_same"] = result["filled_same"] + 1


class BoxComparer:
    """Counts cited fields whose boxes are on the same page with IoU above the threshold in both runs."""

    def __init__(self, calculator: IouCalculator):
        self.calculator = calculator

    def index_boxes(self, citations: list[dict[str, Any]]) -> dict[str, tuple[int, list[float]]]:
        boxes: dict[str, tuple[int, list[float]]] = {}
        for citation in citations:
            bbox = citation.get("bbox")
            if bbox and citation.get("field_path") not in boxes:
                boxes[citation["field_path"]] = (citation.get("page"), bbox)
        return boxes

    def compare_boxes(self, first_citations: list[dict[str, Any]], second_citations: list[dict[str, Any]]) -> dict:
        first_boxes = self.index_boxes(first_citations)
        second_boxes = self.index_boxes(second_citations)
        result = {"paths": 0, "same": 0, "both_cited": 0, "differences": []}
        for path in sorted(set(first_boxes) | set(second_boxes)):
            result["paths"] = result["paths"] + 1
            if path not in first_boxes or path not in second_boxes:
                result["differences"].append((path, "cited in one run only", None))
                continue
            result["both_cited"] = result["both_cited"] + 1
            iou = self.measure_pair(first_boxes[path], second_boxes[path])
            if iou > IOU_THRESHOLD:
                result["same"] = result["same"] + 1
            else:
                result["differences"].append((path, "IoU", round(iou, 2)))
        return result

    def measure_pair(self, first: tuple[int, list[float]], second: tuple[int, list[float]]) -> float:
        if first[0] != second[0]:
            return 0.0
        return self.calculator.compute_iou(first[1], second[1])


class AgreementReporter:
    """Loads two runs, compares them and prints the agreement percentages."""

    def __init__(self):
        self.loader = RunLoader()
        self.value_comparer = ValueComparer(LeafFlattener())
        self.box_comparer = BoxComparer(IouCalculator())

    def report_agreement(self, first_path: Path, second_path: Path, is_listing: bool) -> None:
        first_run = self.loader.load_run(first_path)
        second_run = self.loader.load_run(second_path)
        values = self.value_comparer.compare_values(first_run["data"], second_run["data"])
        boxes = self.box_comparer.compare_boxes(first_run["citations"], second_run["citations"])
        print(f"Values, all fields:            {self.format_share(values['all_same'], values['all_paths'])}")
        print(f"Values, filled in either run:  {self.format_share(values['filled_same'], values['filled_paths'])}")
        print(f"Boxes, cited in either run:    {self.format_share(boxes['same'], boxes['paths'])}")
        print(f"Boxes, cited in both runs:     {self.format_share(boxes['same'], boxes['both_cited'])}")
        if is_listing:
            self.print_differences("Value differences", values["differences"])
            self.print_differences("Box differences", boxes["differences"])

    def format_share(self, same: int, total: int) -> str:
        if total == 0:
            return "n/a"
        return f"{100.0 * same / total:5.1f}%  ({same} of {total})"

    def print_differences(self, title: str, differences: list[tuple]) -> None:
        print(f"\n{title} ({len(differences)}):")
        for path, first, second in differences:
            print(f"  {path}: {first} | {second}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("first_raw_json", type=Path)
    parser.add_argument("second_raw_json", type=Path)
    parser.add_argument("--is_listing", action="store_true", help="also list every difference")
    arguments = parser.parse_args()
    AgreementReporter().report_agreement(arguments.first_raw_json, arguments.second_raw_json, arguments.is_listing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
