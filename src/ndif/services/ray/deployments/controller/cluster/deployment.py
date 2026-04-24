import importlib
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Union

import ray
from opentelemetry import trace

from ......common.providers.mailgun import MailgunProvider
from ......common.providers.objectstore import ObjectStoreProvider
from ......common.providers.socketio import SioProvider
from ......common.tracing import TracingContext, trace_span
from ......common.types import MODEL_KEY
from ...modeling.base import BaseModelDeployment, BaseModelDeploymentArgs

logger = logging.getLogger("ndif")


class DeploymentLevel(Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class Deployment:
    def __init__(
        self,
        model_key: MODEL_KEY,
        deployment_level: DeploymentLevel,
        gpus: dict[int, int],
        size_bytes: int,
        dedicated: bool = False,
        node_id: str = None,
        execution_timeout_seconds: float | None = None,
        actor_class: Optional[Union[str, type]] = None,
    ):
        self.model_key = model_key
        self.deployment_level = deployment_level
        self.gpus = gpus
        self.size_bytes = size_bytes
        self.dedicated = dedicated
        self.node_id = node_id
        self.execution_timeout_seconds = execution_timeout_seconds
        self.actor_class = actor_class
        self.deployed = time.time()

    def _resolve_actor_class(self) -> type[BaseModelDeployment]:
        """Resolve ``self.actor_class`` to a concrete Ray actor class.

        Strings are imported as dotted paths; class objects are returned
        as-is. The controller is expected to have already substituted its
        configured default for any ``None`` at construction time, so a
        ``None`` here is a programming error.
        """
        if self.actor_class is None:
            raise ValueError(
                "actor_class was not set on Deployment — the controller should "
                "have populated it from its default_actor_class before create()."
            )
        if isinstance(self.actor_class, str):
            module_path, _, class_name = self.actor_class.rpartition(".")
            if not module_path:
                raise ValueError(
                    f"actor_class {self.actor_class!r} is not a dotted import path"
                )
            module = importlib.import_module(module_path)
            return getattr(module, class_name)
        return self.actor_class

    @property
    def name(self):
        return f"ModelActor:{self.model_key}"

    @property
    def actor(self):
        return ray.get_actor(self.name, namespace="NDIF")

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the deployment."""

        if self.actor_class is None:
            actor_class_repr = None
        elif isinstance(self.actor_class, str):
            actor_class_repr = self.actor_class
        else:
            actor_class_repr = (
                f"{self.actor_class.__module__}.{self.actor_class.__qualname__}"
            )

        return {
            "model_key": self.model_key,
            "deployment_level": self.deployment_level.value,
            "gpus": self.gpus,
            "size_bytes": self.size_bytes,
            "dedicated": self.dedicated,
            "node_id": self.node_id,
            "execution_timeout_seconds": self.execution_timeout_seconds,
            "actor_class": actor_class_repr,
            "deployed": self.deployed,
        }

    def end_time(self, minimim_deployment_time_seconds: int) -> datetime:
        return datetime.fromtimestamp(
            self.deployed + minimim_deployment_time_seconds, tz=timezone.utc
        )

    def delete(self):
        with trace_span(
            "deployment.delete", attributes={"ndif.model.key": self.model_key}
        ) as span:
            try:
                actor = self.actor
                ray.kill(actor, no_restart=True)
            except Exception:
                span.set_status(trace.StatusCode.ERROR)
                logger.exception(f"Error deleting actor {self.model_key}.")
                pass

    def restart(self):
        with trace_span(
            "deployment.restart", attributes={"ndif.model.key": self.model_key}
        ) as span:
            try:
                actor = self.actor
                ray.kill(actor, no_restart=False)
            except Exception:
                span.set_status(trace.StatusCode.ERROR)
                logger.exception(f"Error restarting actor {self.model_key}.")
                pass

    def cache(self):
        with trace_span(
            "deployment.cache", attributes={"ndif.model.key": self.model_key}
        ) as span:
            try:
                actor = self.actor
                return actor.to_cache.remote(TracingContext.inject())
            except Exception:
                span.set_status(trace.StatusCode.ERROR)
                logger.exception(f"Error adding actor {self.model_key} to cache.")
                return None

    def from_cache(self):
        with trace_span(
            "deployment.from_cache",
            attributes={
                "ndif.model.key": self.model_key,
                "ndif.deploy.gpus": str(self.gpus),
            },
        ) as span:
            try:
                actor = self.actor
                return actor.from_cache.remote(self.gpus, TracingContext.inject())
            except Exception:
                span.set_status(trace.StatusCode.ERROR)
                logger.exception(f"Error removing actor {self.model_key} from cache.")
                return None

    def create(self, node_name: str, deployment_args: BaseModelDeploymentArgs):
        with trace_span(
            "deployment.create",
            attributes={
                "ndif.model.key": self.model_key,
                "ndif.deploy.node": node_name,
                "ndif.deploy.gpus": str(self.gpus),
            },
        ) as span:
            try:
                # Inject the assigned GPU memory allocation so the actor knows which GPUs to target
                deployment_args.gpu_mem_bytes_by_id = self.gpus
                deployment_args.trace_context = TracingContext.inject()

                env_vars = {
                    # Prevent Ray from setting CUDA_VISIBLE_DEVICES, so the actor
                    # inherits full GPU visibility from the worker node. GPU targeting
                    # is handled by max_memory in the actor's load_from_disk/from_cache.
                    "RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES": "1",
                    # Use expandable segments to reduce CUDA memory fragmentation
                    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                    **SioProvider.to_env(),
                    **ObjectStoreProvider.to_env(),
                    **MailgunProvider.to_env(),
                }

                env_vars = {k: v for k, v in env_vars.items() if v is not None}

                actor_class = self._resolve_actor_class()

                actor = actor_class.options(
                    name=self.name,
                    resources={f"node:{node_name}": 0.01},
                    namespace="NDIF",
                    lifetime="detached",
                    runtime_env={
                        "env_vars": env_vars,
                    },
                ).remote(**deployment_args.model_dump())

            except Exception:
                span.set_status(trace.StatusCode.ERROR)
                logger.exception(f"Error creating actor {self.model_key}.")
