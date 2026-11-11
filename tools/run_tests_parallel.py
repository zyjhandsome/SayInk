"""Zero-dependency parallel pytest runner using greedy LPT file sharding.

Splits test files across ``W`` isolated ``pytest`` worker processes without
requiring ``pytest-xdist`` (keeping ``constraints-windows-py310.txt`` offline
and unchanged). Each shard runs with ``QT_QPA_PLATFORM=offscreen``,
``-p no:cacheprovider``, single-threaded BLAS/OpenMP, and its own isolated
temporary directory so parallel shards never contend on ``TMP`` or cache files.

Usage:
    python tools/run_tests_parallel.py
    python tools/run_tests_parallel.py -n 4
    python tools/run_tests_parallel.py tests/test_updater.py tests/test_minutes.py
    python tools/run_tests_parallel.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_HEAVY_KEYWORDS = (
    "settings",
    "meeting",
    "history",
    "speech_recognizer",
    "smoke",
    "overlay",
    "onboarding",
)
_SUMMARY_TOKEN_RE = re.compile(
    r"\b(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|deselected)\b"
)


@dataclass(frozen=True)
class ShardSpec:
    index: int
    targets: tuple[str, ...]
    estimated_weight: int


@dataclass
class ShardResult:
    index: int
    targets: tuple[str, ...]
    returncode: int
    duration_sec: float
    stdout: str
    stderr: str
    counts: dict[str, int] = field(default_factory=dict)


def default_worker_count(cpu_count: int | None = None) -> int:
    """Choose a balanced worker count (amortizes ~1.8s PyQt6/numpy import overhead)."""
    cpus = os.cpu_count() if cpu_count is None else cpu_count
    return min(6, max(1, cpus or 4))


def estimate_target_weight(target: str, *, base_dir: Path = REPO_ROOT) -> int:
    """Estimate relative runtime cost of a test file or node ID."""
    file_part = target.split("::", 1)[0]
    candidate = Path(file_part)
    if not candidate.is_absolute():
        candidate = (base_dir / candidate).resolve()
    try:
        size = max(1024, candidate.stat().st_size)
    except OSError:
        size = 4096
    # Fixed per-file collection/fixture baseline + byte size.
    weight = 3072 + size
    stem = candidate.stem.lower()
    if any(token in stem for token in _HEAVY_KEYWORDS):
        weight = int(weight * 1.35)
    return weight


def discover_test_targets(
    raw_targets: list[str] | None = None,
    *,
    base_dir: Path = REPO_ROOT,
) -> list[str]:
    """Expand directories and explicit test files/node IDs into ordered targets."""
    inputs = raw_targets if raw_targets else ["tests"]
    discovered: list[str] = []
    seen: set[str] = set()

    for raw in inputs:
        node_suffix = ""
        file_part = raw
        if "::" in raw:
            file_part, node_rest = raw.split("::", 1)
            node_suffix = f"::{node_rest}"
        candidate = Path(file_part)
        resolved = candidate if candidate.is_absolute() else (base_dir / candidate)
        if not resolved.exists():
            raise FileNotFoundError(f"Test target does not exist: {raw}")
        if resolved.is_dir():
            if node_suffix:
                raise ValueError(f"Directory target cannot carry pytest node suffix: {raw}")
            files = sorted(
                {
                    *resolved.rglob("test_*.py"),
                    *resolved.rglob("*_test.py"),
                }
            )
            for file_path in files:
                if not file_path.is_file():
                    continue
                try:
                    rel = file_path.resolve().relative_to(base_dir.resolve()).as_posix()
                except ValueError:
                    rel = str(file_path.resolve())
                if rel not in seen:
                    seen.add(rel)
                    discovered.append(rel)
        else:
            try:
                rel_base = resolved.resolve().relative_to(base_dir.resolve()).as_posix()
            except ValueError:
                rel_base = str(resolved.resolve())
            item = f"{rel_base}{node_suffix}"
            if item not in seen:
                seen.add(item)
                discovered.append(item)
    return discovered


def build_shards(
    targets: list[str],
    workers: int,
    *,
    base_dir: Path = REPO_ROOT,
) -> list[ShardSpec]:
    """Partition ``targets`` into load-balanced shards via greedy LPT bin-packing."""
    if not targets:
        return []
    shard_count = max(1, min(int(workers), len(targets)))
    weighted = [
        (estimate_target_weight(target, base_dir=base_dir), target)
        for target in targets
    ]
    weighted.sort(key=lambda pair: (-pair[0], pair[1]))

    bins_targets: list[list[str]] = [[] for _ in range(shard_count)]
    bins_weights: list[int] = [0] * shard_count

    for weight, target in weighted:
        lightest_idx = min(range(shard_count), key=lambda idx: (bins_weights[idx], idx))
        bins_targets[lightest_idx].append(target)
        bins_weights[lightest_idx] += weight

    specs: list[ShardSpec] = []
    for idx in range(shard_count):
        if not bins_targets[idx]:
            continue
        specs.append(
            ShardSpec(
                index=len(specs) + 1,
                targets=tuple(sorted(bins_targets[idx])),
                estimated_weight=bins_weights[idx],
            )
        )
    return specs


def parse_pytest_counts(output: str) -> dict[str, int]:
    """Extract ``passed``, ``failed``, ``skipped``, ``error``, ``deselected`` counts."""
    counts = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "error": 0,
        "xfailed": 0,
        "xpassed": 0,
        "deselected": 0,
    }
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    for line in reversed(lines):
        matches = _SUMMARY_TOKEN_RE.findall(line)
        if not matches:
            continue
        for number_str, label in matches:
            key = "error" if label.startswith("error") else label
            counts[key] = counts.get(key, 0) + int(number_str)
        break
    return counts


def _is_pure_deselection_shard(res: ShardResult) -> bool:
    """True when pytest exited 5 solely because ``-k`` / ``-m`` deselected all shard items."""
    return (
        res.returncode == 5
        and res.counts.get("deselected", 0) > 0
        and res.counts.get("failed", 0) == 0
        and res.counts.get("error", 0) == 0
    )


def _run_single_shard(
    spec: ShardSpec,
    *,
    python_executable: str,
    repo_root: Path,
    temp_root: Path,
    pytest_args: list[str],
    base_env: dict[str, str] | None = None,
) -> ShardResult:
    shard_tmp = temp_root / f"shard_{spec.index:02d}"
    shard_tmp.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("MKL_NUM_THREADS", "1")
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    env.setdefault("NUMEXPR_NUM_THREADS", "1")
    env["TMP"] = str(shard_tmp)
    env["TEMP"] = str(shard_tmp)
    env["TMPDIR"] = str(shard_tmp)

    cmd = [
        python_executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        *spec.targets,
        "-q",
        *pytest_args,
    ]
    t0 = time.perf_counter()
    completed = subprocess.run(
        cmd,
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    elapsed = time.perf_counter() - t0
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return ShardResult(
        index=spec.index,
        targets=spec.targets,
        returncode=completed.returncode,
        duration_sec=elapsed,
        stdout=completed.stdout,
        stderr=completed.stderr,
        counts=parse_pytest_counts(combined),
    )


def run_parallel_pytest(
    targets: list[str],
    *,
    workers: int | None = None,
    pytest_args: list[str] | None = None,
    repo_root: Path = REPO_ROOT,
    python_executable: str = sys.executable,
    quiet: bool = False,
) -> tuple[int, list[ShardResult], float]:
    """Execute ``targets`` across parallel pytest shards and return ``(exit_code, results, elapsed)``."""
    worker_count = default_worker_count() if workers is None else max(1, int(workers))
    specs = build_shards(targets, worker_count, base_dir=repo_root)
    if not specs:
        return 0, [], 0.0

    extra_args = list(pytest_args or [])
    total_shards = len(specs)
    if not quiet:
        print(
            f"Running {len(targets)} test file(s) across {total_shards} parallel shard(s) "
            f"(QT_QPA_PLATFORM=offscreen)...",
            flush=True,
        )

    temp_root = Path(tempfile.mkdtemp(prefix="sayink-pytest-"))
    results: list[ShardResult] = []
    wall_start = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=total_shards) as pool:
            future_map = {
                pool.submit(
                    _run_single_shard,
                    spec,
                    python_executable=python_executable,
                    repo_root=repo_root,
                    temp_root=temp_root,
                    pytest_args=extra_args,
                ): spec
                for spec in specs
            }
            for future in as_completed(future_map):
                res = future.result()
                results.append(res)
                if not quiet:
                    pure_deselect = _is_pure_deselection_shard(res)
                    status = (
                        "OK  "
                        if (res.returncode == 0 or pure_deselect)
                        else f"FAIL({res.returncode})"
                    )
                    passed = res.counts.get("passed", 0)
                    failed = res.counts.get("failed", 0) + res.counts.get("error", 0)
                    skipped = res.counts.get("skipped", 0)
                    deselected = res.counts.get("deselected", 0)
                    detail = f"{passed} passed"
                    if failed:
                        detail += f", {failed} failed/error"
                    if skipped:
                        detail += f", {skipped} skipped"
                    if deselected:
                        detail += f", {deselected} deselected"
                    print(
                        f"  [shard {res.index}/{total_shards}] {status} "
                        f"{len(res.targets):2d} file(s) | {detail} in {res.duration_sec:.2f}s",
                        flush=True,
                    )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    wall_elapsed = time.perf_counter() - wall_start
    results.sort(key=lambda r: r.index)

    failed_shards = [
        r for r in results if r.returncode != 0 and not _is_pure_deselection_shard(r)
    ]
    if failed_shards and not quiet:
        for bad in failed_shards:
            print(
                f"\n{'=' * 72}\n"
                f"FAILED SHARD {bad.index}/{total_shards} (exit {bad.returncode}, {bad.duration_sec:.2f}s)\n"
                f"Targets: {', '.join(bad.targets)}\n"
                f"{'=' * 72}",
                file=sys.stderr,
            )
            if bad.stdout.strip():
                print(bad.stdout.rstrip(), file=sys.stderr)
            if bad.stderr.strip():
                print(bad.stderr.rstrip(), file=sys.stderr)

    totals: dict[str, int] = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "error": 0,
        "xfailed": 0,
        "xpassed": 0,
        "deselected": 0,
    }
    for res in results:
        for key, value in res.counts.items():
            totals[key] = totals.get(key, 0) + value

    selected_total = (
        totals["passed"]
        + totals["failed"]
        + totals["skipped"]
        + totals["error"]
        + totals["xfailed"]
        + totals["xpassed"]
    )

    if not quiet:
        parts = [f"{totals['passed']} passed"]
        if totals["failed"]:
            parts.append(f"{totals['failed']} failed")
        if totals["error"]:
            parts.append(f"{totals['error']} errors")
        if totals["skipped"]:
            parts.append(f"{totals['skipped']} skipped")
        if totals["xfailed"]:
            parts.append(f"{totals['xfailed']} xfailed")
        if totals["deselected"]:
            parts.append(f"{totals['deselected']} deselected")
        print(
            f"\nParallel summary: {', '.join(parts)} across {total_shards} shard(s) "
            f"in {wall_elapsed:.2f}s",
            flush=True,
        )

    if failed_shards:
        exit_code = failed_shards[0].returncode
    elif selected_total == 0 and any(r.returncode == 5 for r in results):
        if not quiet:
            print("No tests matched across any shard.", file=sys.stderr)
        exit_code = 5
    else:
        exit_code = 0
    return exit_code, results, wall_elapsed


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    passthrough: list[str] = []
    if "--" in raw_argv:
        sep = raw_argv.index("--")
        passthrough = raw_argv[sep + 1 :]
        raw_argv = raw_argv[:sep]

    parser = argparse.ArgumentParser(
        description="Run SayInk pytest suite in parallel across balanced file shards.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Test directories, files, or node IDs (default: tests).",
    )
    parser.add_argument(
        "-n",
        "--workers",
        type=int,
        default=default_worker_count(),
        help="Number of parallel pytest worker processes (default: min(6, cpu_count)).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print computed shard assignments without executing pytest.",
    )
    parser.add_argument(
        "-k",
        dest="keyword",
        default="",
        help="Optional pytest -k expression forwarded to every shard.",
    )
    parser.add_argument(
        "--maxfail",
        type=int,
        default=0,
        help="Optional --maxfail=N forwarded to every shard.",
    )
    args = parser.parse_args(raw_argv)

    if args.workers < 1:
        print("error: --workers must be >= 1", file=sys.stderr)
        return 2

    try:
        discovered = discover_test_targets(args.targets, base_dir=REPO_ROOT)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not discovered:
        print("No test files discovered.", file=sys.stderr)
        return 5

    if args.dry_run:
        specs = build_shards(discovered, args.workers, base_dir=REPO_ROOT)
        print(f"Discovered {len(discovered)} test target(s) -> {len(specs)} shard(s):")
        for spec in specs:
            print(
                f"  [shard {spec.index}/{len(specs)}] {len(spec.targets):2d} file(s), "
                f"weight={spec.estimated_weight}: {', '.join(spec.targets)}"
            )
        return 0

    extra_pytest_args = list(passthrough)
    if args.keyword:
        extra_pytest_args.extend(["-k", args.keyword])
    if args.maxfail > 0:
        extra_pytest_args.append(f"--maxfail={args.maxfail}")

    exit_code, _, _ = run_parallel_pytest(
        discovered,
        workers=args.workers,
        pytest_args=extra_pytest_args,
        repo_root=REPO_ROOT,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
