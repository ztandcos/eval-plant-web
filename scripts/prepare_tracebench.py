"""Download a deterministic local-only Tracebench failure sample.

Raw artifacts stay under ignored data/ and are never redistributed by EvalPlant.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

REPO = "https://huggingface.co/datasets/Contextbench/Tracebench/resolve/main"
MANIFEST = "bench_manifest.verified.jsonl"


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_zstd_tar(archive: Path, destination: Path) -> None:
    process = subprocess.Popen(["zstd", "-dc", str(archive)], stdout=subprocess.PIPE)
    if process.stdout is None:
        raise RuntimeError("zstd did not provide archive output")
    with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
        root = destination.resolve()
        for member in tar:
            target = (destination / member.name).resolve()
            if (
                member.islnk()
                or member.issym()
                or (target != root and root not in target.parents)
            ):
                raise ValueError("Unsafe path in Tracebench archive: %s" % member.name)
            tar.extract(member, destination)
    if process.wait() != 0:
        raise RuntimeError("zstd failed to extract %s" % archive)


def select_failures(manifest: Path, limit: int) -> list:
    selected = []
    with manifest.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if (
                row.get("solved") is False
                and row.get("agent") == "mini-SWE-agent"
                and str(row.get("source_relpath") or "").startswith("swe_raw/")
                and row.get("artifact_path")
            ):
                selected.append(row)
    return sorted(selected, key=lambda item: item["traj_id"])[:limit]


def convert_case(row: dict, archive: Path, output: Path) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        extracted = Path(temporary)
        extract_zstd_tar(archive, extracted)
        trajectories = list(extracted.rglob("*.traj.json"))
        test_results = [
            path
            for path in extracted.rglob("*_output.json")
            if path not in trajectories
        ]
        if len(trajectories) != 1:
            raise ValueError("Expected one trajectory in %s" % archive)
        data = json.loads(trajectories[0].read_text(encoding="utf-8"))
        data["task_id"] = row["task_name"]
        data["verdict"] = "FAIL"
        data["source"] = {
            "dataset": "Contextbench/Tracebench",
            "trajectory_id": row["traj_id"],
        }
        case_dir = output / row["traj_id"]
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "trajectory.traj.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tests = []
        if test_results:
            tests = (
                json.loads(test_results[0].read_text(encoding="utf-8")).get("tests")
                or []
            )
        (case_dir / "final_test.log").write_text(
            "\n".join(
                "%s %s" % (item.get("status"), item.get("name")) for item in tests
            )
            + "\n",
            encoding="utf-8",
        )
        (case_dir / "source.json").write_text(
            json.dumps(
                {
                    "source_url": "%s/%s" % (REPO, row["artifact_path"]),
                    "archive_sha256": sha256(archive),
                    "manifest": row,
                    "note": (
                        "incorrect_stages are source annotations, not EvalPlant "
                        "gold labels"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/public/tracebench"))
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.limit < 1:
        raise ValueError("--limit must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / MANIFEST
    if not manifest.exists():
        download("%s/%s" % (REPO, MANIFEST), manifest)
    selected = select_failures(manifest, args.limit)
    if len(selected) < args.limit:
        raise ValueError("Only %s matching failures are available" % len(selected))
    for position, row in enumerate(selected, start=1):
        archive = args.output / "archives" / Path(row["artifact_path"]).name
        if not archive.exists():
            download("%s/%s" % (REPO, row["artifact_path"]), archive)
        convert_case(row, archive, args.output / "cases")
        print("[%s/%s] %s" % (position, len(selected), row["traj_id"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
