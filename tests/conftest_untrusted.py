"""Force every request in the suite through a sandbox runner.

Enabled with `-p conftest_untrusted`. nnsight has no `trusted` on its request
envelope -- the server reads it from the request body, where an API key would
normally stamp it -- so this injects it, which is what
docs/developing/testing.md describes as the way to reach this path with auth off.
"""
import json

from nnsight.schema.request import RequestModel

_original = RequestModel.metadata


def _untrusted(self):
    body = json.loads(_original(self))
    body["trusted"] = False
    return json.dumps(body)


RequestModel.metadata = _untrusted
