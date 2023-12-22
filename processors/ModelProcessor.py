from typing import Dict, Any

import accelerate
import torch
import nnsight
from nnsight import util
from nnsight.logger import logger as engine_logger
from nnsight.pydantics import JobStatus, RequestModel, ResponseModel, tracing
import importlib
from ..ResponseDict import ResponseDict
from . import Processor


class ModelProcessor(Processor):
    """
    Handles the LLM inference processing.

    Attributes
    ----------
        model_name_or_path : str
            repo id of hugging face LLM model repository or path to pre-cached checkpoint directory.
        device_map : Dict
            mapping of model modules to specific devices. To be used by accelerate if max_memory is None.
        max_memory : Dict[int,str]
            mapping of device to max allowed memory. To be used by accelerate to generate device_map.
        response_dict : ResponseDict
    """

    def __init__(
        self,
        model_name_or_path: str,
        nnsight_class: str,
        model_kwargs: Dict[str, Any],
        max_memory: Dict[int, str],
        response_dict: ResponseDict,
        *args,
        **kwargs,
    ):
        self.model_name_or_path = model_name_or_path
        self.nnsight_class = nnsight_class
        self.model_kwargs = model_kwargs
        self.max_memory = max_memory
        self.response_dict = response_dict

        super().__init__(*args, **kwargs)

    def initialize(self) -> None:
        # Create Model

        #temp
        *module_atoms, cls = self.nnsight_class.split('.')
        module = importlib.import_module('.'.join(module_atoms))
        nnsight_class = getattr(module, cls)

        if nnsight_class is nnsight.LanguageModel:
            self.model_kwargs['torch_dtype'] = torch.bfloat16

        self.model = nnsight_class(self.model_name_or_path, dispatch=True, **self.model_kwargs)

        if nnsight_class is not  nnsight.LanguageModel:
            self.model.local_model = self.model.local_model.half()
            
        # If max_memory is set, use accelerate.infer_auto_device_map to get a device_map
        if self.max_memory is not None:
            self.model.meta_model.tie_weights()
            self.device_map = accelerate.infer_auto_device_map(
                self.model.meta_model, max_memory=self.max_memory
            )

        super().initialize()

        mem_params = sum(
            [
                param.nelement() * param.element_size()
                for param in self.model.local_model.parameters()
            ]
        )
        mem_bufs = sum(
            [
                buf.nelement() * buf.element_size()
                for buf in self.model.local_model.buffers()
            ]
        )
        mem_gbs = (mem_params + mem_bufs) * 1e-9

        self.logger.debug(
            f"MEM: {self.model_name_or_path} size {mem_gbs:.2f}GBs"
        )

    def tensor_sizeof_bytes(self, data):
        mem_bytes = 0

        def sizeof(tensor: torch.Tensor):
            nonlocal mem_bytes
            mem_bytes += tensor.nelement() * tensor.element_size()

        util.apply(data, sizeof, torch.Tensor)

        return mem_bytes

    def process(self, request: RequestModel) -> None:
        try:
            args, kwargs = request.args, request.kwargs

            graph = request.intervention_graph

            # Run model with parameters and interventions
            # with torch.profiler.profile(
            #     activities=[
            #         torch.profiler.ProfilerActivity.CPU,
            #         torch.profiler.ProfilerActivity.CUDA,
            #     ],
            #     record_shapes=True,
            #     profile_memory=True,
            # ) as prof:
            #     with torch.profiler.record_function("model_execution"):
            output = self.model(
                self.model._generation
                if request.generation
                else self.model._forward,
                request.batched_input,
                graph,
                *args,
                **kwargs,
                    )

            # self.logger.debug(
            #     "MEM: \n"
            #     + prof.key_averages().table(sort_by="cpu_time_total", row_limit=10)
            # )

            # Create response
            response = ResponseModel(
                id=request.id,
                recieved=request.received,
                blocking=request.blocking,
                status=JobStatus.COMPLETED,
                description="Your job has been completed.",
                output=util.apply(output, lambda x: x.detach().cpu(), torch.Tensor),
                # Move all copied data to cpu
                saves={
                    name: util.apply(value.value, lambda x: x.detach().cpu(), torch.Tensor)
                    for name, value in graph.nodes.items()
                    if value is not None
                },
            ).log(self.logger)

            self.response_dict[request.id] = response

            mem_output_mbytes = self.tensor_sizeof_bytes(response.output) * 1e-6
            mem_saves_mbytes = self.tensor_sizeof_bytes(response.saves) * 1e-6

            self.logger.info(
                f"MEM: output size {mem_output_mbytes:.2f}MBs; saves size {mem_saves_mbytes:.2f}MBs; total size {mem_output_mbytes + mem_saves_mbytes:.2f}MBs"
            )

        except Exception as exception:
            self.response_dict[request.id] = ResponseModel(
                id=request.id,
                recieved=request.received,
                blocking=request.blocking,
                status=JobStatus.ERROR,
                description=str(exception),
            ).log(self.logger)

            raise exception
