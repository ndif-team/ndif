
import nnsight

nnsight.CONFIG.API.HOST = "localhost:5001"
model = nnsight.LanguageModel("gpt2")

import time

for i in range(3):
    start = time.time()
    with model.generate(remote=True) as runner:
        with runner.invoke([[10] * 20] * 1000):
            hs = model.transformer.h[-1].output.save()

    print(hs.value)
    print(time.time() - start)
