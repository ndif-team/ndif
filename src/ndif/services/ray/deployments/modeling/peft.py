import ray

from nnsight.schema.request import RequestModel
from peft import PeftModel
from nnsight.intervention.envoy import Envoy
from .base import BaseModelDeployment


@ray.remote(num_cpus=2, num_gpus=0, max_restarts=-1)
class PEFTModelActor(BaseModelDeployment):
    """Ray remote actor for PEFT-style model execution.

    Reads ``peft_repo_id`` from the request's ``extras`` dict. If present,
    the underlying HF model is wrapped in a ``PeftModel`` via
    ``PeftModel.from_pretrained`` so the adapter is active during execution.
    On cleanup the wrapper's ``unload()`` is called to strip the adapter
    modules back off, and the original base module is restored so the shared
    model is adapter-free for the next caller.
    """

    def pre(self) -> RequestModel:
        self._peft_active = False

        repo_id = (self.request.extras or {}).get("peft_repo_id")

        if repo_id:
            peft_model = PeftModel.from_pretrained(self.model._module, repo_id)
            Envoy.__init__(
                self.model,
                peft_model.get_base_model(),
                interleaver=self.model.interleaver,
            )
            self.persistent_objects = self.model._remoteable_persistent_objects()
            self._peft_active = True

        return super().pre()

    def cleanup(self) -> None:
        if getattr(self, "_peft_active", False):
            try:
                Envoy.__init__(
                    self.model,
                    self.model._module.unload(),
                    interleaver=self.model.interleaver,
                )
                self.persistent_objects = self.model._remoteable_persistent_objects()
            except Exception:
                self.logger.exception("Failed to unload PEFT adapter")
            self._peft_active = False

        super().cleanup()
