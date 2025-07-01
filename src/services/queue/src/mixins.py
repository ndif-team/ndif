from typing import Optional
from socketio import SimpleClient
import boto3

"""Mixins which are used by multiple classes."""

class NetworkingMixin:
    """Mixin to provide networking capabilities (sio and object_store) to classes."""
    
    def __init__(self, sio: Optional[SimpleClient] = None, object_store: Optional[boto3.client] = None):
        self._sio = sio
        self._object_store = object_store
    

    @property
    def sio(self) -> Optional[SimpleClient]:
        """Get the socketio client."""
        return self._sio
    

    @property
    def object_store(self) -> Optional[boto3.client]:
        """Get the object store client."""
        return self._object_store
    

    def has_networking_clients(self) -> bool:
        """Check if networking clients are available."""
        return self._sio is not None and self._object_store is not None