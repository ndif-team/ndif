"""Force every request in the suite through a sandbox runner.

Enabled with `-p conftest_untrusted`. nnsight has no `tags` on its request
envelope -- the server reads them from the request body, where an API key would
normally stamp them -- so this injects an empty set, which is what
docs/developing/testing.md describes as the way to reach this path with auth off.
"""
import json

from nnsight.schema.request import RequestModel

_original = RequestModel.metadata


def _untrusted(self):
    body = json.loads(_original(self))
    body["tags"] = []
    return json.dumps(body)


RequestModel.metadata = _untrusted
