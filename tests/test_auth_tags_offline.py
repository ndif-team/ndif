"""Offline tests for what the API key's tags grant — no Postgres, no server.

``trusted`` runs the caller's code next to the weights and may deploy a model
whose key asks for ``trust_remote_code``; the ``trust_remote_code`` tag grants
only the latter.
"""

import asyncio

import pytest

from ndif.common.schema.request import BackendRequestModel
from ndif.services.api import auth
from ndif.services.api.auth import TRUST_REMOTE_CODE_TAG, TRUSTED_TAG, Identity


def _identity(*tags):
    return Identity(key_id="k", user_id="u", email="a@b", user_tags=list(tags))


class TestIdentity:
    def test_the_trust_remote_code_tag_grants_the_deploy_permission_only(self):
        identity = _identity(TRUST_REMOTE_CODE_TAG)
        assert identity.trust_remote_code is True
        assert identity.trusted is False

    def test_trusted_implies_it(self):
        identity = _identity(TRUSTED_TAG)
        assert identity.trusted is True
        assert identity.trust_remote_code is True

    def test_an_untagged_key_gets_neither(self):
        identity = _identity()
        assert identity.trusted is False
        assert identity.trust_remote_code is False


def _envelope(**fields):
    return BackendRequestModel(model_key="m", **fields).model_dump_json(exclude_unset=True)


def _validate(monkeypatch, identity, **fields):
    async def verify(api_key):
        return identity

    monkeypatch.setattr(auth, "verify_api_key", verify)
    return asyncio.run(auth.validate_request(data=_envelope(**fields), api_key="key"))


class TestValidateRequestWithAuthOn:
    def test_the_trust_remote_code_tag_stamps_only_that_flag(self, monkeypatch):
        request = _validate(monkeypatch, _identity(TRUST_REMOTE_CODE_TAG))
        assert request.trust_remote_code is True
        assert request.trusted is False, "the sandbox stays on for this key"

    def test_the_trusted_tag_stamps_both(self, monkeypatch):
        request = _validate(monkeypatch, _identity(TRUSTED_TAG))
        assert request.trusted is True
        assert request.trust_remote_code is True

    def test_a_client_cannot_grant_itself_either(self, monkeypatch):
        request = _validate(monkeypatch, _identity(), trusted=True, trust_remote_code=True)
        assert request.trusted is False
        assert request.trust_remote_code is False


class TestValidateRequestWithAuthOff:
    def test_an_unspecified_request_is_trusted_and_so_may_deploy_repo_code(self, monkeypatch):
        request = _validate(monkeypatch, None)
        assert request.trusted is True
        assert request.trust_remote_code is True

    def test_opting_out_of_trusted_opts_out_of_both(self, monkeypatch):
        request = _validate(monkeypatch, None, trusted=False)
        assert request.trusted is False
        assert request.trust_remote_code is False
