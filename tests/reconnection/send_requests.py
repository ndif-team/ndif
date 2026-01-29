#!/usr/bin/env python3
"""
Send requests to the NDIF API for testing reconnection behavior.

Usage:
    python send_requests.py --count 10           # Send 10 requests
    python send_requests.py --continuous         # Run continuously
    python send_requests.py --workers 3          # Use 3 concurrent workers
"""

import argparse
import sys
import time
import threading
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add nnsight to path if needed
sys.path.insert(0, "/disk/u/jadenfk/wd/nnsight/src")

from nnsight import LanguageModel, CONFIG


def timestamp():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def send_request(request_id: int, model_name: str = "openai-community/gpt2") -> dict:
    """Send a single request and return the result."""
    start_time = time.time()
    result = {
        "request_id": request_id,
        "success": False,
        "error": None,
        "duration": 0,
        "output": None,
    }

    try:
        print(f"[{timestamp()}] Request {request_id}: Starting...")

        model = LanguageModel(model_name)

        # Use model.trace() directly - simpler and works reliably
        with model.trace("Hello world", remote=True):
            hs = model.transformer.h[-1].output[0].save()

        result["success"] = True
        result["output"] = f"shape: {hs.shape}"
        print(
            f"[{timestamp()}] Request {request_id}: SUCCESS in {time.time() - start_time:.2f}s"
        )

    except Exception as e:
        result["error"] = str(e)
        print(f"[{timestamp()}] Request {request_id}: FAILED - {e}")
        traceback.print_exc()

    result["duration"] = time.time() - start_time
    return result


def run_batch(count: int, workers: int = 1):
    """Run a batch of requests."""
    print(f"\n{'='*60}")
    print(f"[{timestamp()}] Starting batch of {count} requests with {workers} workers")
    print(f"{'='*60}\n")

    results = {"success": 0, "failed": 0, "errors": []}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(send_request, i): i for i in range(count)}

        for future in as_completed(futures):
            result = future.result()
            if result["success"]:
                results["success"] += 1
            else:
                results["failed"] += 1
                results["errors"].append(result["error"])

    print(f"\n{'='*60}")
    print(
        f"[{timestamp()}] Batch complete: {results['success']} success, {results['failed']} failed"
    )
    if results["errors"]:
        print(f"Errors: {set(results['errors'])}")
    print(f"{'='*60}\n")

    return results


def run_continuous(interval: float, workers: int = 1):
    """Run requests continuously until interrupted."""
    print(f"\n{'='*60}")
    print(
        f"[{timestamp()}] Starting continuous mode (interval={interval}s, workers={workers})"
    )
    print(f"Press Ctrl+C to stop")
    print(f"{'='*60}\n")

    request_id = 0
    stats = {"success": 0, "failed": 0}

    try:
        while True:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = []
                for _ in range(workers):
                    futures.append(executor.submit(send_request, request_id))
                    request_id += 1

                for future in as_completed(futures):
                    result = future.result()
                    if result["success"]:
                        stats["success"] += 1
                    else:
                        stats["failed"] += 1

            # Print running stats
            total = stats["success"] + stats["failed"]
            success_rate = (stats["success"] / total * 100) if total > 0 else 0
            print(
                f"[{timestamp()}] Stats: {stats['success']}/{total} success ({success_rate:.1f}%)"
            )

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n[{timestamp()}] Stopped by user")
        print(f"Final stats: {stats['success']} success, {stats['failed']} failed")


def main():
    parser = argparse.ArgumentParser(description="Send requests to NDIF API")
    parser.add_argument(
        "--count", type=int, default=5, help="Number of requests to send"
    )
    parser.add_argument("--continuous", action="store_true", help="Run continuously")
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Interval between requests (continuous mode)",
    )
    parser.add_argument(
        "--workers", type=int, default=1, help="Number of concurrent workers"
    )
    parser.add_argument(
        "--host", type=str, default="http://localhost:5001", help="API host URL"
    )
    parser.add_argument(
        "--model", type=str, default="openai-community/gpt2", help="Model to use"
    )

    args = parser.parse_args()

    # Configure API host
    CONFIG.API.HOST = args.host
    print(f"[{timestamp()}] Using API host: {CONFIG.API.HOST}")
    print(f"[{timestamp()}] Using model: {args.model}")

    if args.continuous:
        run_continuous(args.interval, args.workers)
    else:
        run_batch(args.count, args.workers)


if __name__ == "__main__":
    main()
