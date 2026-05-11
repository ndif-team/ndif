import ray

from nnsight.schema.request import RequestModel
from peft import PeftModel
from nnsight.intervention.envoy import Envoy
from .base import BaseModelDeployment


@ray.remote(num_cpus=2, num_gpus=0, max_restarts=-1)
class PEFTModelActor(BaseModelDeployment):
    """Ray remote actor for PEFT-style model execution.

    Reads ``peft_repo_id`` from the request's ``extras`` dict and tracks the
    currently-loaded adapter across requests. The adapter is only swapped when
    the requested ``peft_repo_id`` differs from the one already loaded, so
    back-to-back requests for the same adapter reuse it in place rather than
    paying the load/unload cost every call.

    Transitions handled in ``pre()``:

        current  requested  action
        -------  ---------  ------
        None     None       no-op
        None     X          load X
        X        X          no-op
        X        Y          unload X, load Y
        X        None       unload X
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.current_peft_repo_id: str | None = None

    def pre(self) -> RequestModel:
        requested = (self.request.extras or {}).get("peft_repo_id")

        if requested != self.current_peft_repo_id:
            if self.current_peft_repo_id is not None:
                try:
                    Envoy.__init__(
                        self.model,
                        self.model._module.unload(),
                        interleaver=self.model.interleaver,
                    )
                except Exception:
                    self.logger.exception("Failed to unload PEFT adapter")
                self.current_peft_repo_id = None

            if requested:
                peft_model = PeftModel.from_pretrained(self.model._module, requested)
                Envoy.__init__(
                    self.model,
                    peft_model,
                    interleaver=self.model.interleaver,
                )
                self.current_peft_repo_id = requested

            self.persistent_objects = self.model._remoteable_persistent_objects()

        return super().pre()
