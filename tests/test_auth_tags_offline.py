"""Offline tests for what the API key's tags grant — no Postgres, no server.

Two tags reach the model-deploy path: ``trusted`` (the caller's own code runs
next to the weights, and it may deploy a checkpoint that runs its own code) and
``trust_remote_code`` (only the latter). Either one lets the request provision a
model whose key asks for ``trust_remote_code``; only ``trusted`` skips the
sandbox.
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
    def test_an_unspecified_request_is_trusted_and_may_deploy_repo_code(self, monkeypatch):
        request = _validate(monkeypatch, None)
        assert request.trusted is True
        assert request.trust_remote_code is True

    def test_opting_out_of_trusted_opts_out_of_both(self, monkeypatch):
        request = _validate(monkeypatch, None, trusted=False)
        assert request.trusted is False
        assert request.trust_remote_code is False

    def test_the_client_may_grant_the_deploy_permission_alone(self, monkeypatch):
        # The dev-mode way to exercise an untrusted caller that may still deploy
        # a checkpoint running its own code.
        request = _validate(monkeypatch, None, trusted=False, trust_remote_code=True)
        assert request.trusted is False
        assert request.trust_remote_code is True


class TestItReachesTheProcessor:
    def test_the_processor_records_the_deploy_permission_not_trusted(self):
        # The gate in `Processor.start` compares the key against the request's
        # deploy permission, which either tag grants -- so the processor must
        # record `request.trust_remote_code`, not `request.trusted`, or a
        # trust_remote_code-tagged key could never deploy the model its tag is
        # for.
        import inspect

        from ndif.services.api.queue import processor

        assert "self.ensure_started(request.trust_remote_code)" in inspect.getsource(
            processor.Processor.enqueue
        )
        source = inspect.getsource(processor.Processor.start)
        assert "trust_remote_code_of(self.model_key) and not self.trust_remote_code" in source
