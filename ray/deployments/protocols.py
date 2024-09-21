
from nnsight.tracing.Node import Node
from nnsight.tracing.protocols import Protocol

class LogProtocol(Protocol):
    
    
    @classmethod
    def execute(cls, node: Node):
        return super().execute(node)