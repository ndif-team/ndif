"""trust_remote_code in the model key, offline.

The client writes ``"trust_remote_code": true`` into the key when it builds a
model with that option. The server reads it back off the key (``key_field``),
mints the same key from the CLI and dashboard, and lets only a request tagged
for it provision such a model (``Processor.start``).
"""

import asyncio
from pathlib import Path

from ndif.cli.lib._common import normalize_specs
from ndif.cli.lib.model_config import build_models_list, load_model_config
from ndif.common.schema.controller import DeployResponse, ModelDeployResult, ReplicaStates
from ndif.common.schema.request import BackendRequestModel
from ndif.common.types import key_field

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
        assert key_field(_TRUSTING, "trust_remote_code") is True

    def test_a_key_without_the_field(self):
        assert key_field(_PLAIN, "trust_remote_code") is None

    def test_a_non_json_suffix(self):
        assert key_field("nnsight.modeling.vllm.vllm.VLLM:x/y", "trust_remote_code") is None
        assert key_field("", "task") is None


class TestWhoMayProvisionIt:
    """Only a request with the permission provisions a key asking for repo code."""

    @staticmethod
    def _run(monkeypatch, model_key, tags):
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
                    return self.result(*args) if callable(self.result) else self.result

                return answer()

        def scale(model_key, n, config):
            scaled.append(model_key)
            return DeployResponse(results={model_key: ModelDeployResult(replicas=["r"])})

        controller = type("Controller", (), {})()
        controller.get_deployment = Call(ReplicaStates(replicas=[]))
        controller.scale = Call(scale)

        # The processor asks what is running; the replica asks for the deploy.
        monkeypatch.setattr(processor_module, "controller_handle", lambda: controller)
        monkeypatch.setattr(replica_module, "controller_handle", lambda: controller)
        monkeypatch.setattr(replica_module.Replica, "wait", lambda self: asyncio.sleep(0))
        monkeypatch.setattr(replica_module.Replica, "start", lambda self: None)

        async def main():
            processor = Processor(model_key, asyncio.Queue())

            async def reply(*args, **kwargs):
                return None

            async def purge(message=None):
                purged.append(message)

            processor.reply = reply
            processor.purge = purge
            processor.queue.put(BackendRequestModel(model_key=model_key, tags=tags))
            processor.status = ProcessorStatus.PROVISIONING
            await processor.start()
            processor.status = ProcessorStatus.CANCELLED
            processor.autoscaling_task.cancel()

        asyncio.run(main())
        return scaled, purged

    def test_an_untagged_request_is_refused_before_the_controller_deploys(self, monkeypatch):
        scaled, purged = self._run(monkeypatch, _TRUSTING, set())

        assert scaled == []
        assert len(purged) == 1 and "trust_remote_code" in purged[0] and "trusted" in purged[0]

    def test_the_trust_remote_code_permission_alone_deploys_it(self, monkeypatch):
        scaled, purged = self._run(monkeypatch, _TRUSTING, {"trust_remote_code"})

        assert scaled == [_TRUSTING] and purged == []

    def test_an_ordinary_key_needs_no_permission(self, monkeypatch):
        scaled, purged = self._run(monkeypatch, _PLAIN, set())

        assert scaled == [_PLAIN] and purged == []


class TestMintingTheKey:
    def test_normalize_specs(self):
        assert normalize_specs([{"checkpoint": "x/custom", "trust_remote_code": True}])[0]["trust_remote_code"] is True
        assert normalize_specs([{"checkpoint": "gpt2"}])[0]["trust_remote_code"] is False

    def test_models_yaml(self, tmp_path: Path):
        config = tmp_path / "models.yaml"
        config.write_text("models:\n  - gpt2\n  - checkpoint: x/custom\n    trust_remote_code: true\n")
        plain, custom = load_model_config(config)
        assert plain["trust_remote_code"] is False
        assert custom["trust_remote_code"] is True
        (spec,) = load_model_config(config, default_trust_remote_code=True)[:1]
        assert spec["trust_remote_code"] is True

    def test_export_round_trip(self):
        (entry,) = build_models_list([{"repo_id": "x/custom", "trust_remote_code": True}])
        assert entry == {"checkpoint": "x/custom", "trust_remote_code": True}
        (plain,) = build_models_list([{"repo_id": "gpt2"}])
        assert plain == "gpt2"

    def test_the_cli_flag(self):
        from ndif.cli.commands.deploy import deploy
        from ndif.cli.commands.evict import evict
        from ndif.cli.commands.restart import restart
        from ndif.cli.commands.scale import scale

        for command in (deploy, scale, evict, restart):
            names = {opt for p in command.params for opt in p.opts}
            assert "--trust-remote-code" in names and "--trusted" not in names
