
import nnsight

nnsight.CONFIG.API.HOST = "localhost:5001"
nnsight.CONFIG.API.APIKEY = "0Bb6oUQxj2TuPtlrTkny"
model = nnsight.LanguageModel("gpt2")

import time

for i in range(3):
    start = time.time()
    with model.generate(remote=True) as runner:
        with runner.invoke('hello'):
            hs = model.transformer.h[-1].output.save()

    print(hs.value)
    print(time.time() - start)
