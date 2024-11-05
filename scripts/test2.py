
import nnsight
from nnsight.schema.request import RequestModel
from nnsight import CONFIG
import gc
CONFIG.set_default_api_key("GZj3dV4aWltBJAQ6qsHL")

model = nnsight.LanguageModel("meta-llama/Llama-2-70b-hf")

with model.trace('test', remote=True) as runner:
    #runner._graph.module_proxy.requires_grad_(False)
    output = model.output.save()

print(output)

# model = nnsight.LanguageModel("mistralai/Mixtral-8x7B-Instruct-v0.1")

# with model.trace('test', remote=True) as runner:
#     output = model.output.save()

# print(output)

model = nnsight.LanguageModel("EleutherAI/gpt-j-6b")

with model.trace('test', remote=True) as runner:
    output = model.output.save()

print(output)
        
model = nnsight.LanguageModel("meta-llama/Meta-Llama-3-70B")

with model.trace('test', remote=True) as runner:
    #runner._graph.module_proxy.requires_grad_(False)
    output = model.output.save()

print(output)

