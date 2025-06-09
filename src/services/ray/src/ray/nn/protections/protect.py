

def protected_getattr(object, name):
    
    if name in object.__dict__:
        return object.__dict__[name]
    else:
        raise AttributeError(f"Attribute {name} not found in {object}")
    
def protected_setattr(object, name, value):


def protect(object):
    
    class ProtectedObject(object.__class__):
        pass
    
    protected_object = ProtectedObject()
    
    protected_object.__dict__[:] = object.__dict__[:]
    
    
    
    
        