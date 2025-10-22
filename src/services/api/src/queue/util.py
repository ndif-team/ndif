

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
            
    
    