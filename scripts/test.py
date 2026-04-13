import nnsight

nnsight.CONFIG.set_default_api_key("12345678-1234-5678-1234-567812345678")
nnsight.CONFIG.API.HOST = "http://localhost:5001"

model = nnsight.LanguageModel("openai-community/gpt2")

with model.trace("The Eiffel Tower is located in ", remote=True):
    output = model.output.save()

print(output)
