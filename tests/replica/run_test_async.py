#!/usr/bin/env python3
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path


async def run_once(run_index: int, script_path: str, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        started_at = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        ended_at = time.perf_counter()

    return {
        "run_index": run_index,
        "returncode": proc.returncode,
        "duration_s": ended_at - started_at,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


async def run_many(script_path: str, runs: int, concurrency: int) -> list[dict]:
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        asyncio.create_task(run_once(i + 1, script_path, semaphore))
        for i in range(runs)
    ]
    return await asyncio.gather(*tasks)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a Python script multiple times asynchronously and record timings."
    )
    parser.add_argument(
        "--script",
        default="test.py",
        help="Script to run (default: test.py).",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=200,
        help="Total number of runs to execute (default: 5).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=20,
        help="Maximum number of concurrent runs (default: 5).",
    )
    parser.add_argument(
        "--save-json",
        default="",
        help="Optional path to save full run results as JSON.",
    )
    args = parser.parse_args()

    if args.runs <= 0:
        raise ValueError("--runs must be > 0")
    if args.concurrency <= 0:
        raise ValueError("--concurrency must be > 0")

    script_path = str(Path(args.script).expanduser().resolve())
    if not Path(script_path).exists():
        raise FileNotFoundError(f"Script not found: {script_path}")

    print(f"Running {args.runs} runs of {script_path}")
    print(f"Concurrency: {min(args.concurrency, args.runs)}")

    batch_start = time.perf_counter()
    results = asyncio.run(run_many(script_path, args.runs, min(args.concurrency, args.runs)))
    batch_end = time.perf_counter()
    batch_duration = batch_end - batch_start

    success_count = sum(1 for item in results if item["returncode"] == 0)
    failed_count = args.runs - success_count

    durations = [item["duration_s"] for item in results]
    avg_duration = sum(durations) / len(durations)
    min_duration = min(durations)
    max_duration = max(durations)

    print("\nPer-run durations:")
    for item in sorted(results, key=lambda x: x["run_index"]):
        status = "ok" if item["returncode"] == 0 else f"failed({item['returncode']})"
        print(f"  run {item['run_index']:>3}: {item['duration_s']:.3f}s  {status}")

    print("\nSummary:")
    print(f"  total runs: {args.runs}")
    print(f"  success: {success_count}")
    print(f"  failed: {failed_count}")
    print(f"  batch end-to-end time: {batch_duration:.3f}s")
    print(f"  per-run min/avg/max: {min_duration:.3f}s / {avg_duration:.3f}s / {max_duration:.3f}s")

    if failed_count:
        print("\nFailed run output snippets:")
        for item in sorted(results, key=lambda x: x["run_index"]):
            if item["returncode"] == 0:
                continue
            print(f"\n--- run {item['run_index']} stderr ---")
            print(item["stderr"].strip() or "(empty)")

    if args.save_json:
        out_path = Path(args.save_json).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "script": script_path,
                    "runs": args.runs,
                    "concurrency": min(args.concurrency, args.runs),
                    "batch_duration_s": batch_duration,
                    "results": results,
                },
                f,
                indent=2,
            )
        print(f"\nSaved JSON results to: {out_path}")


if __name__ == "__main__":
    # before running the test, deploy the models with desired replica count
    main()
