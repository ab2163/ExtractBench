"""Run the uncertainty survey once for a document and save it, so later extraction runs can reuse it.

The survey's session is kept and its working folder snapshotted, so the "resume" pipeline can fork the same agent,
and the "fresh" pipeline can hand its uncertainties.json to a new agent.

Usage:
    uv run python scripts/make_shared_survey.py --file data/short/passcoag-2020-w2-p0003-r3.pdf
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from extract_bench.inference.providers.extract.claude_code_uncertainty_extract import (  # noqa: E402
    SHARED_SURVEY_CONFIG,
    SharedSurveyMaker,
    SharedSurveyStore,
)
from extract_bench.test_cases.loader import load_test_case  # noqa: E402


class SharedSurveyRunner:
    """Loads the document's schema, runs the shared survey and renders its trace page."""

    def run_shared_survey(self, pdf_path: Path, survey_root: Path) -> dict:
        test_case = load_test_case(pdf_path)
        if test_case is None:
            raise ValueError(f"No test.json found next to {pdf_path}")
        maker = SharedSurveyMaker("claude_code_extract", dict(SHARED_SURVEY_CONFIG))
        store = SharedSurveyStore(survey_root, pdf_path.stem)
        record = maker.make_survey(pdf_path, maker._prepare_schema(test_case.data_schema), store)
        self.render_trace(store)
        return record

    def render_trace(self, store: SharedSurveyStore) -> None:
        script = Path(__file__).resolve().parent / "render_trace.py"
        subprocess.run([sys.executable, str(script), str(store.folder / "survey.raw.json"), "--output_dir",
                        str(store.folder)], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, required=True, help="document PDF with its .test.json alongside")
    parser.add_argument("--survey_root", type=Path, default=Path(SHARED_SURVEY_CONFIG["survey_root"]))
    arguments = parser.parse_args()
    record = SharedSurveyRunner().run_shared_survey(arguments.file.resolve(), arguments.survey_root)
    value_count = len(record["uncertainties"].get("value_uncertainties") or [])
    box_count = len(record["uncertainties"].get("box_uncertainties") or [])
    print(f"session {record['session_id']}: {value_count} value and {box_count} box uncertainties, "
          f"cost {record['cost_usd']}, scratch folder {record['scratch_dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
