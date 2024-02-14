import torch

import nnsight
from nnsight.pydantics.Request import RequestModel

model = nnsight.LanguageModel("meta-llama/Llama-2-70b-hf")

with model.trace('test', remote=True) as runner:
    output = model.output.save()

print(output)

model = nnsight.LanguageModel("meta-llama/Llama-2-13b-hf")

with model.trace('test', remote=True) as runner:
    output = model.output.save()

print(output)

model = nnsight.LanguageModel("EleutherAI/gpt-j-6b")

with model.trace('test', remote=True) as runner:
    output = model.output.save()

print(output)
        
       
        
       


    
