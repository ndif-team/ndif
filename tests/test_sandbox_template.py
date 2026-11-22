"""A runner whose parent has gone exits.

No server and no Ray: a real runner process, started the way `host.spawn` starts
one.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from ndif.services.ray.sandbox import host


def test_a_runner_whose_parent_is_already_gone_exits():
    # The pid a runner is given is the parent it watches. Read from getppid()
    # after the imports instead, a parent that died during them had already
    # been replaced by init, which the runner then watched forever.
    path = os.path.join(tempfile.mkdtemp(), "runner.sock")
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    runner = subprocess.Popen(
        [sys.executable, "-m", "ndif.services.ray.sandbox.runner",
         path, "1", str(gone.pid)],
        env={**host.runner_env(), "PYTHONPATH": host._PACKAGE_ROOT},
    )
    try:
        assert runner.wait(timeout=60) == 1
    finally:
        runner.kill()
        if os.path.exists(path):
            os.remove(path)
