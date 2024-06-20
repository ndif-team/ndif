import torch

import nnsight
from nnsight.pydantics.Request import RequestModel

model = nnsight.LanguageModel("meta-llama/Llama-2-70b-hf")

false_prompt = """\
The city of Tokyo is in Japan. This statement is: TRUE
The city of Hanoi is in Poland. This statement is: FALSE
The city of Chicago is in Canada. This statement is:"""
print('asd')
with model.forward(remote=True, remote_include_output=False, validate=False) as runner:
    with runner.invoke(false_prompt, scan=False):
        
        x = model.lm_head.output.save()
x.value

breakpoint()
# for i in range(3):
#     start = time.time()
#     with model.generate(remote=True) as runner:
#         with runner.invoke([[10] * 10] * 20):
#             hs = model.model.layers[0].output[0].softmax(dim=-1).save()

#     print(hs.value)
#     print(time.time() - start)

            



    
