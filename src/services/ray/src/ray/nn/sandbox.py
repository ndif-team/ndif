import inspect
from nnsight.intervention.tracing.util import ExceptionWrapper, wrap_exception

def run(tracer, fn):
    __nnsight_tracing_info__ = tracer.info
    _frame = inspect.currentframe()
    tracer.info.frame = _frame
    
    for mediator in tracer.mediators:
        
        mediator.info.frame = _frame
    
    try:
        tracer.execute(fn)
    except Exception as e:
        raise wrap_exception(e,tracer.info) from None
