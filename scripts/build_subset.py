"""Sample a reproducible ExtractBench subset and copy its files into a separate input directory.

Usage:
    uv run python scripts/build_subset.py sample --out subsets/pilot_70.json --seed 7 --short 25 --medium 25 --long 20
    uv run python scripts/build_subset.py materialize --manifest subsets/pilot_70.json --dest data_pilot_70
    uv run extract-bench run <pipeline> --input_dir data_pilot_70 --output_dir output_pilot_70
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sys
from datetime import date
from pathlib import Path

DATA_DIR = Path("data")
SPLITS = ("short", "medium", "long")
TEST_JSON_SUFFIX = ".test.json"
MANIFEST_NAME = ".extract_bench_manifest.json"


class FileHasher:
    """Computes SHA-256 checksums of files."""

    def hash_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()


class DocumentLocator:
    """Finds the test.json and source document files for a test case stem."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    def list_stems(self, split: str) -> list[str]:
        stems = []
        for test_json in sorted((self.data_dir / split).glob(f"*{TEST_JSON_SUFFIX}")):
            stems.append(test_json.name[: -len(TEST_JSON_SUFFIX)])
        return stems

    def find_source_file(self, split: str, stem: str) -> Path:
        for candidate in sorted((self.data_dir / split).glob(f"{stem}.*")):
            if not candidate.name.endswith(TEST_JSON_SUFFIX):
                return candidate
        raise FileNotFoundError(f"no source document for {split}/{stem}")

    def find_test_json(self, split: str, stem: str) -> Path:
        return self.data_dir / split / f"{stem}{TEST_JSON_SUFFIX}"


class SubsetSampler:
    """Draws a seeded, per-split random sample of test cases and records it as a manifest."""

    def __init__(self, locator: DocumentLocator, hasher: FileHasher):
        self.locator = locator
        self.hasher = hasher

    def read_revision(self) -> str:
        manifest_path = self.locator.data_dir / MANIFEST_NAME
        if not manifest_path.exists():
            return "unknown"
        return json.loads(manifest_path.read_text()).get("revision", "unknown")

    def sample_split(self, split: str, count: int, rng: random.Random) -> list[str]:
        stems = self.locator.list_stems(split)
        if count > len(stems):
            raise ValueError(f"{split} has {len(stems)} documents, cannot sample {count}")
        chosen = rng.sample(stems, count)
        return sorted(chosen)

    def describe_document(self, split: str, stem: str) -> dict:
        source = self.locator.find_source_file(split, stem)
        test_json = self.locator.find_test_json(split, stem)
        return {
            "test_id": f"{split}/{stem}",
            "split": split,
            "stem": stem,
            "source_file": source.name,
            "test_json_sha256": self.hasher.hash_file(test_json),
            "source_sha256": self.hasher.hash_file(source),
        }

    def build_manifest(self, name: str, seed: int, counts: dict[str, int]) -> dict:
        rng = random.Random(seed)
        documents = []
        for split in SPLITS:
            for stem in self.sample_split(split, counts[split], rng):
                documents.append(self.describe_document(split, stem))
        return {
            "name": name,
            "seed": seed,
            "created": date.today().isoformat(),
            "dataset_repo": "llamaindex/ExtractBench",
            "dataset_revision": self.read_revision(),
            "per_split": counts,
            "documents": documents,
        }


class SubsetMaterializer:
    """Copies a manifest's files into a split-structured input directory and verifies their checksums."""

    def __init__(self, locator: DocumentLocator, hasher: FileHasher):
        self.locator = locator
        self.hasher = hasher

    def copy_checked(self, source: Path, target: Path, expected_sha: str) -> None:
        actual_sha = self.hasher.hash_file(source)
        if actual_sha != expected_sha:
            raise ValueError(f"checksum mismatch for {source}: dataset differs from the manifest")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def materialize(self, manifest: dict, dest: Path) -> int:
        copied = 0
        for document in manifest["documents"]:
            split, stem = document["split"], document["stem"]
            source = self.locator.data_dir / split / document["source_file"]
            test_json = self.locator.find_test_json(split, stem)
            self.copy_checked(source, dest / split / source.name, document["source_sha256"])
            self.copy_checked(test_json, dest / split / test_json.name, document["test_json_sha256"])
            copied += 1
        return copied


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a reproducible ExtractBench subset.")
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample")
    sample.add_argument("--out", type=Path, required=True)
    sample.add_argument("--seed", type=int, required=True)
    for split in SPLITS:
        sample.add_argument(f"--{split}", type=int, required=True)
    materialize = commands.add_parser("materialize")
    materialize.add_argument("--manifest", type=Path, required=True)
    materialize.add_argument("--dest", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> None:
    arguments = parse_arguments(argv)
    locator = DocumentLocator(DATA_DIR)
    hasher = FileHasher()
    if arguments.command == "sample":
        counts = {"short": arguments.short, "medium": arguments.medium, "long": arguments.long}
        manifest = SubsetSampler(locator, hasher).build_manifest(arguments.out.stem, arguments.seed, counts)
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"wrote {len(manifest['documents'])} documents to {arguments.out}")
        return
    manifest = json.loads(arguments.manifest.read_text())
    copied = SubsetMaterializer(locator, hasher).materialize(manifest, arguments.dest)
    print(f"copied {copied} documents into {arguments.dest}")


if __name__ == "__main__":
    main(sys.argv[1:])
