

def patch():
    
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
            
    
    