
import nnsight
from nnsight import CONFIG

CONFIG.API.HOST = "localhost:5001"
CONFIG.API.SSL = False

model = nnsight.LanguageModel("openai-community/gpt2")

with model.trace("ayy", remote=True):
    
    output = model.output.save()
    
print(output)
