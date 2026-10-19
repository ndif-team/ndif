"""What a request's tags grant, offline: no Postgres, no server.

``validate_request`` stamps the key's user_tags onto the request; the request's
``trusted`` / ``trust_remote_code`` / ``priority`` properties read them.
"""

import asyncio

from ndif.common.schema.request import BackendRequestModel
from ndif.services.api import auth
from ndif.services.api.auth import Identity


class TestTheProperties:
    def test_trust_remote_code_alone(self):
        request = BackendRequestModel(model_key="m", tags={"trust_remote_code"})
        assert request.trust_remote_code and not request.trusted and not request.priority

    def test_trusted_implies_trust_remote_code(self):
        request = BackendRequestModel(model_key="m", tags={"trusted"})
        assert request.trusted and request.trust_remote_code

    def test_no_tags(self):
        request = BackendRequestModel(model_key="m")
        assert not (request.trusted or request.trust_remote_code or request.priority)


def _validate(monkeypatch, identity, **fields):
    async def verify(api_key):
        return identity

    monkeypatch.setattr(auth, "verify_api_key", verify)
    data = BackendRequestModel(model_key="m", **fields).model_dump_json(exclude_unset=True)
    return asyncio.run(auth.validate_request(data=data, api_key="key"))


class TestValidateRequest:
    def test_auth_on_stamps_the_keys_tags_over_the_clients(self, monkeypatch):
        identity = Identity(key_id="k", user_id="u", user_tags=["priority"])
        request = _validate(monkeypatch, identity, tags={"trusted"})
        assert request.tags == {"priority"}

    def test_auth_off_defaults_to_trusted(self, monkeypatch):
        assert _validate(monkeypatch, None).tags == {"trusted"}

    def test_auth_off_honors_explicit_tags(self, monkeypatch):
        assert _validate(monkeypatch, None, tags=set()).tags == set()
