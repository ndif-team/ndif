import torch

import nnsight
from nnsight.pydantics.Request import RequestModel

model = nnsight.LanguageModel("meta-llama/Llama-2-70b-hf")

with model.forward(remote=True, remote_include_output=True) as runner:
    with runner.invoke('test'):
        pass

print(runner.output)

model = nnsight.LanguageModel("meta-llama/Llama-2-13b-hf")

with model.forward(remote=True, remote_include_output=True) as runner:
    with runner.invoke('test'):
        pass

print(runner.output)

model = nnsight.LanguageModel("EleutherAI/gpt-j-6b")

with model.forward(remote=True, remote_include_output=True) as runner:
    with runner.invoke('test'):
        pass

print(runner.output)
        
       
        
       


    
