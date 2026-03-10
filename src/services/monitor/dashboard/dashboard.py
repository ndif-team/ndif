#!/usr/bin/env python3
"""Dashboard server for NDIF monitor logs."""

import argparse
import json
from pathlib import Path

from flask import Flask, jsonify, send_file

app = Flask(__name__)
LOG_DIR = None


def parse_log_files(pattern):
    entries = []
    for f in sorted(LOG_DIR.glob(pattern)):
        for line in f.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


@app.route("/")
def index():
    return send_file(Path(__file__).parent / "dashboard.html")


@app.route("/api/connected")
def api_connected():
    return jsonify(parse_log_files("connected_*.log"))


@app.route("/api/models")
def api_models():
    return jsonify(parse_log_files("models_*.log"))



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    LOG_DIR = Path(args.log_dir)
    app.run(host=args.host, port=args.port, debug=False)
