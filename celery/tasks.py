import celery
from amqp import exceptions
from celery import bootsteps
from celery.utils.log import get_task_logger
from click import Option

from ..pydantics import RequestModel, ResponseModel
from . import celeryconfig, customconfig
from .process_request import validate_request

logger = get_task_logger(__name__)

app = celery.Celery("tasks")

app.user_options["worker"].add(Option(["--repo_id"], default=None))

app.user_options["worker"].add(
    Option(
        ["--api_url"],
        default=None,
    )
)

app.user_options["worker"].add(
    Option(
        ["--allowed_modules"],
        default="",
    )
)

# Model.
# Only gets populated if `repo_id` custom argument is defined.
model = None


class CustomArgs(bootsteps.StartStopStep):
    def __init__(self, worker, repo_id, api_url, allowed_modules, **options):
        customconfig.repo_id = repo_id
        customconfig.api_url = api_url
        customconfig.allowed_modules = allowed_modules.split(",")

        if customconfig.repo_id is not None:
            import nnsight

            global model

            model = nnsight.LanguageModel(
                customconfig.repo_id, dispatch=True, device_map="auto"
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


app.steps["worker"].add(CustomArgs)
app.config_from_object(celeryconfig)


@app.task()
def run_model(request: RequestModel):
    """Task for `model` workers to run. Executes a model and interleaves an intervention graph.
    Depends on `torch` and `nnsight`

    Args:
        request (RequestModel): _description_

    Raises:
        exception: _description_
    """
    try:
        # Import
        import torch

        from nnsight import util

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
            recieved=request.received,
            blocking=request.blocking,
            status=ResponseModel.JobStatus.COMPLETED,
            description="Your job has been completed.",
            output=util.apply(output, lambda x: x.detach().cpu(), torch.Tensor),
            # Move all copied data to cpu
            saves={
                name: util.apply(value.value, lambda x: x.detach().cpu(), torch.Tensor)
                for name, value in request.intervention_graph.nodes.items()
                if value is not None
            },
        ).log(logger).update_backend(app.backend._get_connection()).blocking_response(
            customconfig.api_url
        )

    except Exception as exception:
        ResponseModel(
            id=request.id,
            session_id=request.session_id,
            received=request.received,
            blocking=request.blocking,
            status=ResponseModel.JobStatus.ERROR,
            description=str(exception),
        ).log(logger).update_backend(app.backend._get_connection()).blocking_response(
            customconfig.api_url
        )

        raise exception


@app.task()
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
        # TODO use manager. This requires a rabbitmq manager image
        with app.broker_connection() as connection:
            with connection.channel() as channel:
                try:
                    queue = channel.queue_declare(queue_name, passive=True)
                except exceptions.NotFound:
                    raise ValueError(
                        f"Model with id '{request.repo_id}' not among hosted models."
                    )

            # Validate request.
            validate_request(request)

            # Have model workers for this model process the request.
            run_model.apply_async(
                [request], queue=queue_name, connection=connection
            ).forget()

        ResponseModel(
            id=request.id,
            session_id=request.session_id,
            received=request.received,
            blocking=request.blocking,
            status=ResponseModel.JobStatus.APPROVED,
            description="Your job was approved and is waiting to be run.",
        ).log(logger).update_backend(app.backend._get_connection()).blocking_response(
            customconfig.api_url
        )

    except Exception as exception:
        ResponseModel(
            id=request.id,
            session_id=request.session_id,
            received=request.received,
            blocking=request.blocking,
            status=ResponseModel.JobStatus.ERROR,
            description=str(exception),
        ).log(logger).update_backend(app.backend._get_connection()).blocking_response(
            customconfig.api_url
        )

        raise exception
