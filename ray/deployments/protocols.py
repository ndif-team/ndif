from typing import Callable

from nnsight.contexts.backends.RemoteBackend import RemoteMixin
from nnsight.schema.format.functions import update_function
from nnsight.tracing.Node import Node
from nnsight.tracing.protocols import Protocol, StreamingDownloadProtocol, StreamingUploadProtocol


class LogProtocol(Protocol):

    @classmethod
    def set(cls, fn: Callable):

        update_function(print, fn)


class ServerStreamingDownloadProtocol(StreamingDownloadProtocol):

    send: Callable

    @classmethod
    def set(cls, fn: Callable):

        cls.send = fn

        update_function(StreamingDownloadProtocol, cls)

    @classmethod
    def execute(cls, node: Node):

        data = RemoteMixin.remote_stream_format(node)
        
        value = node.args[0].value

        cls.send((data, value))

        node.set_value(None)

class ServerStreamingUploadProtocol(StreamingUploadProtocol):
    
    get:Callable
    
    @classmethod
    def set(cls, fn:Callable):
        
        cls.get = fn
        
        update_function(StreamingUploadProtocol, ServerStreamingUploadProtocol)
        
        
    @classmethod
    def execute(cls, node: Node):
        
        value = cls.get(node)
        
        node.set_value(value)