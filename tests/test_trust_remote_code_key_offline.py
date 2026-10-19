"""Offline tests for trust_remote_code's place in the model key — no server needed.

The client writes ``"trust_remote_code": true`` into the key when it builds a
model with that option, and the server reads it back: to decide whether the
request provisioning the model may do so, to show it in a status, and to mint
the same key from the CLI and dashboard so an admin's deploy is the deployment
such a client reaches.
"""

import asyncio
from pathlib import Path

from ndif.cli.lib._common import normalize_specs
from ndif.cli.lib.model_config import build_models_list, load_model_config
from ndif.common.types import trust_remote_code_of
from ndif.common.schema.controller import DeployResponse, ModelDeployResult, ReplicaStates

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


class TestTheControllerIsNotToldSeparately:
    """The key is the one channel; a second one is a second opinion."""

    def test_the_deploy_config_has_no_trust_field(self):
        from ndif.common.schema.controller import DeploymentConfig

        assert "trusted" not in DeploymentConfig.model_fields
        assert "trust_remote_code" not in DeploymentConfig.model_fields

    def test_the_actor_args_have_none_either(self):
        from ndif.services.ray.deployments.modeling.base import BaseModelDeploymentArgs

        assert "trust_remote_code" not in BaseModelDeploymentArgs.model_fields

    def test_the_evaluator_takes_none(self):
        import inspect

        from ndif.services.ray.deployments.controller.cluster.evaluator import ModelEvaluator

        for name in ("__call__", "max_tp", "prefetch", "actor_class", "_entry"):
            params = inspect.signature(getattr(ModelEvaluator, name)).parameters
            assert "trust_remote_code" not in params, name

    def test_status_reads_it_off_the_key(self):
        import inspect

        from ndif.services.ray.deployments.controller.controller import _ControllerActor

        source = inspect.getsource(_ControllerActor.status)
        assert "trust_remote_code_of(deployment.model_key)" in source
        assert '"trusted"' not in source


class TestWhoMayProvisionIt:
    """A key asking for the checkpoint's own code is deployed only by a tagged caller.

    The check lives in the API's ``Processor.start``, at the moment it learns
    the controller has nothing running and is about to deploy: a model someone
    with the permission already deployed is open to everyone, and the
    controller is never asked to size a key the caller may not deploy, since
    even that runs the repo's config code.
    """

    @staticmethod
    def _run(monkeypatch, model_key, permitted):
        from ndif.services.api.queue import processor as processor_module
        from ndif.services.api.queue import replica as replica_module
        from ndif.services.api.queue.processor import Processor, ProcessorStatus

        scaled = []
        purged = []

        class Call:
            def __init__(self, result):
                self.result = result

            def remote(self, *args):
                async def answer():
                    if callable(self.result):
                        return self.result(*args)
                    return self.result

                return answer()

        def scale(model_key, n, config):
            scaled.append(model_key)
            return DeployResponse(results={model_key: ModelDeployResult(replicas=["r"])})

        class Controller:
            pass

        controller = Controller()
        controller.get_deployment = Call(ReplicaStates(replicas=[]))
        controller.scale = Call(scale)

        # Both modules bind the name separately; the processor asks what is
        # running, the replica asks for the deploy.
        monkeypatch.setattr(processor_module, "controller_handle", lambda: controller)
        monkeypatch.setattr(replica_module, "controller_handle", lambda: controller)
        # Stop at the controller's answer: this test is about whether the
        # deploy is attempted, not whether a stub replica comes up.
        monkeypatch.setattr(replica_module.Replica, "wait", lambda self: asyncio.sleep(0))
        monkeypatch.setattr(replica_module.Replica, "start", lambda self: None)

        async def main():
            processor = Processor(model_key, asyncio.Queue())
            processor.trust_remote_code = permitted

            async def reply(*args, **kwargs):
                return None

            async def purge(message=None):
                purged.append(message)

            processor.reply = reply
            processor.purge = purge
            processor.status = ProcessorStatus.PROVISIONING
            await processor.start()
            processor.status = ProcessorStatus.CANCELLED
            processor.autoscaling_task.cancel()

        asyncio.run(main())
        return scaled, purged

    def test_an_unpermitted_request_is_refused_before_the_controller_deploys(self, monkeypatch):
        scaled, purged = self._run(monkeypatch, _TRUSTING, permitted=False)

        assert scaled == [], "the controller must not be asked to deploy (or size) the key"
        assert len(purged) == 1
        assert "trust_remote_code" in purged[0]
        assert "trusted" in purged[0], "the message names both tags that would allow it"

    def test_a_permitted_request_deploys_it(self, monkeypatch):
        scaled, purged = self._run(monkeypatch, _TRUSTING, permitted=True)

        assert scaled == [_TRUSTING]
        assert purged == []

    def test_an_ordinary_key_needs_no_permission(self, monkeypatch):
        scaled, purged = self._run(monkeypatch, _PLAIN, permitted=False)

        assert scaled == [_PLAIN]
        assert purged == []


class TestSpecPlumbing:
    def test_normalize_specs_carries_it(self):
        specs = normalize_specs([{"checkpoint": "x/custom", "trust_remote_code": True}])
        assert specs[0]["trust_remote_code"] is True

    def test_normalize_specs_defaults_it_off(self):
        specs = normalize_specs([{"checkpoint": "gpt2"}])
        assert specs[0]["trust_remote_code"] is False

    def test_trusted_is_no_longer_a_spec_field(self):
        specs = normalize_specs([{"checkpoint": "gpt2", "trusted": True}])
        assert "trusted" not in specs[0]

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

    def test_the_cli_flags(self):
        from ndif.cli.commands.deploy import deploy
        from ndif.cli.commands.evict import evict
        from ndif.cli.commands.restart import restart
        from ndif.cli.commands.scale import scale

        # Every command that mints a key from a checkpoint name takes it, or it
        # could not name a deployment made with it.
        for command in (deploy, scale, evict, restart):
            names = {opt for p in command.params for opt in p.opts}
            assert "--trust-remote-code" in names
            assert "--trusted" not in names
