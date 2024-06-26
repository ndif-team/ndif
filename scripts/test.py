
import nnsight

model = nnsight.LanguageModel("openai-community/gpt2")

nnsight.CONFIG.set_default_api_key('0Bb6oUQxj2TuPtlrTkny')

with model.trace("ayy", remote=True):
    
    output = model.output.save()
    
