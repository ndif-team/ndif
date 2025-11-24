import nnsight

nnsight.CONFIG.set_default_api_key("api key")
nnsight.CONFIG.API.HOST = "localhost:5001"
nnsight.CONFIG.API.SSL = False

model = nnsight.LanguageModel("openai-community/gpt2")

with model.trace("The Eiffel Tower is located in ", remote=True):
    output = model.output.save()

print(output)
