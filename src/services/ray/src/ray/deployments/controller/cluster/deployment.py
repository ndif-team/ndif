from enum import Enum

from ... import MODEL_KEY


class DeploymentLevel(Enum):
    
    DEDICATED = "dedicated"
    HOT = "hot"
    
    WARM = "warm"
    COLD = "cold"

class Deployment:
    
    def __init__(self, model_key:MODEL_KEY, deployment_level:DeploymentLevel, gpus_required:int):
        
        self.model_key = model_key
        self.deployment_level = deployment_level
        self.gpus_required = gpus_required
        
        
        
