"""Offline tests for trust_remote_code's place in the model key — no server needed.

The client writes ``"trust_remote_code": true`` into the key when it builds a
model with that option, and the server reads it back: to refuse the key from
an untrusted caller, to show it in a status, and to mint the same key from the
CLI and dashboard so an admin's deploy is the deployment such a client reaches.
"""

from pathlib import Path

from ndif.cli.lib._common import normalize_specs
from ndif.cli.lib.model_config import build_models_list, load_model_config
from ndif.common.model_key import trust_remote_code_of
from ndif.common.schema.controller import DeploymentConfig
from ndif.services.ray.deployments.controller.cluster.cluster import untrusted_remote_code

_PLAIN = (
    "nnsight.modeling.transformers.TransformersModel:"
    '{"repo_id": "openai-community/gpt2", "revision": null, "task": "text-generation"}'
)
_TRUSTING = (
    "nnsight.modeling.transformers.TransformersModel:"
    '{"repo_id": "x/custom", "revision": null, "task": "text-generation", '
    '"trust_remote_code": true}'
)


class TestReadingTheKey:
    def test_a_key_that_asks_for_it(self):
        assert trust_remote_code_of(_TRUSTING) is True

    def test_a_key_without_the_field_is_an_ordinary_checkpoint(self):
        assert trust_remote_code_of(_PLAIN) is False

    def test_an_explicit_false_is_false(self):
        assert trust_remote_code_of(_PLAIN.replace('"task"', '"trust_remote_code": false, "task"')) is False

    def test_a_non_json_suffix_is_false(self):
        # A VLLM key's suffix is the bare repo id.
        assert trust_remote_code_of("nnsight.modeling.vllm.vllm.VLLM:x/y") is False

    def test_garbage_is_false(self):
        assert trust_remote_code_of("") is False
        assert trust_remote_code_of("no-colon-here") is False


class TestWhoMayDeployIt:
    """The key says what the model is; the config says who is asking."""

    def test_an_untrusted_caller_is_refused(self):
        reason = untrusted_remote_code(_TRUSTING, DeploymentConfig(trusted=False))
        assert reason is not None
        assert "trust_remote_code" in reason
        assert _TRUSTING in reason, "the refusal names the key, so the caller knows which"

    def test_a_trusted_caller_may(self):
        assert untrusted_remote_code(_TRUSTING, DeploymentConfig(trusted=True)) is None

    def test_an_ordinary_key_needs_no_trust(self):
        assert untrusted_remote_code(_PLAIN, DeploymentConfig(trusted=False)) is None


class TestSpecPlumbing:
    def test_normalize_specs_carries_it(self):
        specs = normalize_specs([{"checkpoint": "x/custom", "trust_remote_code": True}])
        assert specs[0]["trust_remote_code"] is True

    def test_normalize_specs_defaults_it_off(self):
        specs = normalize_specs([{"checkpoint": "gpt2"}])
        assert specs[0]["trust_remote_code"] is False

    def test_it_is_separate_from_trusted(self):
        # `trusted` is who is asking; this is what the model is. An admin's
        # deploy of gpt2 is trusted and must not mint a trust_remote_code key,
        # or no ordinary client would reach it.
        specs = normalize_specs([{"checkpoint": "gpt2", "trusted": True}])
        assert specs[0]["trusted"] is True
        assert specs[0]["trust_remote_code"] is False

    def test_model_config_field(self, tmp_path: Path):
        config = tmp_path / "models.yaml"
        config.write_text(
            "models:\n"
            "  - gpt2\n"
            "  - checkpoint: x/custom\n"
            "    trust_remote_code: true\n"
        )
        plain, custom = load_model_config(config)
        assert plain["trust_remote_code"] is False
        assert custom["trust_remote_code"] is True

    def test_model_config_default_from_the_cli_flag(self, tmp_path: Path):
        config = tmp_path / "models.yaml"
        config.write_text("models:\n  - x/custom\n")
        (spec,) = load_model_config(config, default_trust_remote_code=True)
        assert spec["trust_remote_code"] is True

    def test_it_round_trips_through_the_serializer(self):
        (entry,) = build_models_list([{"repo_id": "x/custom", "trust_remote_code": True}])
        assert entry == {"checkpoint": "x/custom", "trust_remote_code": True}
        (plain,) = build_models_list([{"repo_id": "gpt2"}])
        assert plain == "gpt2"
