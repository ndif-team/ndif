#!/usr/bin/env python3
"""Capture git information at build time and output as JSON."""

import json
import subprocess
from datetime import datetime, timezone
from typing import Optional


def run_git_command(args: list[str]) -> Optional[str]:
    """Run a git command and return its output, or None if it fails."""
    try:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def is_working_tree_dirty() -> bool:
    """Check if the working tree has uncommitted changes."""
    try:
        # Check both unstaged and staged changes
        subprocess.run(
            ["git", "diff", "--quiet"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            check=True,
            capture_output=True,
        )
        return False
    except subprocess.CalledProcessError:
        return True
    except FileNotFoundError:
        return False


def capture_git_info() -> dict:
    """Capture git repository information."""
    commit = run_git_command(["rev-parse", "HEAD"]) or "unknown"
    commit_short = run_git_command(["rev-parse", "--short", "HEAD"]) or "unknown"
    branch = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"
    tag = run_git_command(["describe", "--tags", "--exact-match"]) or ""
    commit_date = run_git_command(["log", "-1", "--format=%cI"]) or "unknown"
    commit_author = run_git_command(["log", "-1", "--format=%an <%ae>"]) or "unknown"
    commit_message = run_git_command(["log", "-1", "--format=%s"]) or "unknown"
    dirty = is_working_tree_dirty()
    build_date = datetime.now(timezone.utc).isoformat()

    return {
        "git": {
            "commit": commit,
            "commit_short": commit_short,
            "branch": branch,
            "tag": tag,
            "dirty": dirty,
            "commit_date": commit_date,
            "commit_author": commit_author,
            "commit_message": commit_message,
        },
        "build": {
            "date": build_date,
        },
    }


def main():
    """Main entry point - capture git info and print as JSON."""
    info = capture_git_info()
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
