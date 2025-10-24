import time

def cache_maintainer(clear_time: int):
    """
    A function decorator that clears lru_cache clear_time seconds
    :param clear_time: In seconds, how often to clear cache (only checks when called)
    """
    def inner(func):
        def wrapper(*args, **kwargs):
            if hasattr(func, 'next_clear'):
                if time.time() > func.next_clear:
                    func.cache_clear()
                    func.next_clear = time.time() + clear_time
            else:
                func.next_clear = time.time() + clear_time

            return func(*args, **kwargs)
        return wrapper
    return inner

def patch():
    # We patch the _async_send method to avoid a nasty deadlock bug in Ray.
    # If a seperate thread derefernces a ClientObjectRef during an async send, both are vying for the same lock.
    # Therefore we prevent deleting the ClientObjectRef until the async send is complete.
    # Should probably just `delay` the deletion until the async send is complete, not prevent it entirely.
    from ray.util.client import dataclient, common
    
    original_async_send = dataclient.DataClient._async_send
      
    def _async_send(_self, req, callback = None):
        
        original_ref_deletion = common.ClientObjectRef.__del__
        
        common.ClientObjectRef.__del__ = lambda self: None
        
        try:
            original_async_send(_self, req, callback)
        finally:
            common.ClientObjectRef.__del__ = original_ref_deletion
            
    dataclient.DataClient._async_send = _async_send