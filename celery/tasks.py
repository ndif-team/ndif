import functools
import gc
import inspect
from functools import wraps

import celery
import torch
from amqp import exceptions
from celery import bootsteps, worker
from celery.utils.log import get_task_logger
from click import Option

import nnsight
from nnsight import util
from nnsight.pydantics import RequestModel
from nnsight.pydantics.format import FUNCTIONS_WHITELIST, get_function_name
from nnsight.pydantics.format.types import FUNCTION, FunctionWhitelistError
from nnsight.tracing.Proxy import Proxy

from ..pydantics import ResponseModel, ResultModel
from . import celeryconfig, customconfig

logger = get_task_logger(__name__)

app = celery.Celery("tasks")

app.user_options["worker"].add(Option(["--repo_id"], default=None))
app.user_options["worker"].add(Option(["--model_kwargs"], default=None))
app.user_options["worker"].add(
    Option(
        ["--api_url"],
        default=None,
    )
)

# Model.
# Only gets populated if `repo_id` custom argument is defined.
model = None


class CustomArgs(bootsteps.StartStopStep):
    def __init__(
        self,
        worker: worker.WorkController,
        repo_id,
        model_kwargs,
        api_url,
        **options,
    ):
        customconfig.repo_id = repo_id
        customconfig.model_kwargs = model_kwargs
        customconfig.api_url = api_url

        if customconfig.repo_id is not None:
            model_kwargs = (
                eval(customconfig.model_kwargs)
                if customconfig.model_kwargs is not None
                else {}
            )

            global model

            model = nnsight.LanguageModel(
                customconfig.repo_id, dispatch=True, device_map="auto", **model_kwargs
            )

            mem_params = sum(
                [
                    param.nelement() * param.element_size()
                    for param in model.local_model.parameters()
                ]
            )
            mem_bufs = sum(
                [
                    buf.nelement() * buf.element_size()
                    for buf in model.local_model.buffers()
                ]
            )
            mem_gbs = (mem_params + mem_bufs) * 1e-9

            logger.info(f"MEM: {customconfig.repo_id} size {mem_gbs:.2f}GBs")

            def info_wrapper(fn):
                @wraps(fn)
                def inner(*args, **kwargs):
                    info: dict = fn(*args, **kwargs)

                    info["custom_info"] = {
                        "repo_id": repo_id,
                        "config_json_string": model.local_model.config.to_json_string()
                        if hasattr(model.local_model, "config")
                        else None,
                        "kwargs": {
                            key: str(value) for key, value in model_kwargs.items()
                        },
                    }

                    return info

                return inner

            worker.info = info_wrapper(worker.info)

            def whitelist_proxy_call(callable: FUNCTION, *args, **kwargs):
                fn_name = get_function_name(callable)
                if fn_name in FUNCTIONS_WHITELIST:
                    return callable(*args, **kwargs)

                obj = (
                    callable.__self__
                    if not isinstance(callable, functools.partial)
                    else callable.args[0]
                )

                func = (
                    callable
                    if not isinstance(callable, functools.partial)
                    else callable.func
                )

                if isinstance(obj, torch.nn.Module) and "forward" in str(func):
                    return callable(*args, **kwargs)

                raise FunctionWhitelistError(
                    f"Function with name `{callable.__qualname__}` not in function whitelist."
                )

            Proxy.proxy_call = whitelist_proxy_call


app.steps["worker"].add(CustomArgs)
app.config_from_object(celeryconfig)


@app.task(ignore_result=True)
def run_model(request: RequestModel):
    """Task for `model` workers to run. Executes a model and interleaves an intervention graph.
    Depends on `torch` and `nnsight`

    Args:
        request (RequestModel): _description_

    Raises:
        exception: _description_
    """

    output = None

    try:
        # Execute model with intervention graph.
        output = model(
            model._generation if request.generation else model._forward,
            request.batched_input,
            request.intervention_graph,
            *request.args,
            **request.kwargs,
        )

        ResponseModel(
            id=request.id,
            session_id=request.session_id,
            received=request.received,
            status=ResponseModel.JobStatus.COMPLETED,
            description="Your job has been completed.",
            result=ResultModel(
                id=request.id,
                output=util.apply(output, lambda x: x.detach().cpu(), torch.Tensor)
                if request.include_output
                else None,
                # Move all copied data to cpu
                saves={
                    name: util.apply(
                        node.value, lambda x: x.detach().cpu(), torch.Tensor
                    )
                    for name, node in request.intervention_graph.nodes.items()
                    if node.value is not inspect._empty
                },
            ),
        ).log(logger).save(app.backend._get_connection()).blocking_response(
            customconfig.api_url
        )

    except Exception as exception:
        ResponseModel(
            id=request.id,
            session_id=request.session_id,
            received=request.received,
            status=ResponseModel.JobStatus.ERROR,
            description=str(exception),
        ).log(logger).save(app.backend._get_connection()).blocking_response(
            customconfig.api_url
        )

    # Cleanup

    del request
    del output

    model.local_model.zero_grad()
    
    gc.collect()
    torch.cuda.empty_cache()


@app.task(ignore_result=True)
def process_request(request: RequestModel):
    """Task for `request` workers to run. Validates requests.
    Checks if intervention graph and inputs use approved modules in their pickled data. Set by ENV variable.
    Checks if queue for a model service exists for specified model.

    Args:
        request (RequestModel): _description_

    Raises:
        ValueError: _description_
        exception: _description_
    """
    try:
        # Model workers should listen on a queue with name in the format: models-<huggingface repo id>
        queue_name = f"models-{request.repo_id}"

        # Check if a queue for this model services exists.
        # If not, raise error and inform user.
        # TODO use manager. This requires a rabbitmq manager image.
        with app.broker_connection() as connection:
            with connection.channel() as channel:
                try:
                    queue = channel.queue_declare(queue_name, passive=True)
                except exceptions.NotFound:
                    raise ValueError(
                        f"Model with id '{request.repo_id}' not among hosted models."
                    )

            # Compile request.
            request.compile()

            # Have model workers for this model process the request.
            run_model.apply_async(
                [request], queue=queue_name, connection=connection
            ).forget()

        ResponseModel(
            id=request.id,
            session_id=request.session_id,
            received=request.received,
            status=ResponseModel.JobStatus.APPROVED,
            description="Your job was approved and is waiting to be run.",
        ).log(logger).save(app.backend._get_connection()).blocking_response(
            customconfig.api_url
        )

    except Exception as exception:
        ResponseModel(
            id=request.id,
            session_id=request.session_id,
            received=request.received,
            status=ResponseModel.JobStatus.ERROR,
            description=str(exception),
        ).log(logger).save(app.backend._get_connection()).blocking_response(
            customconfig.api_url
        )
