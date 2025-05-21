import nnsight

nnsight.CONFIG.set_default_api_key("03dd83e1-56d4-4f96-9c13-a7ecb991c210")
nnsight.CONFIG.API.HOST = "localhost:5001"
nnsight.CONFIG.API.SSL = False

model = nnsight.LanguageModel("openai-community/gpt2")

with model.trace("NDIF on AWS is really ", remote=True):
    
    output = model.output.save()
    
print(output)
