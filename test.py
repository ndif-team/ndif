
import nnsight

model = nnsight.LanguageModel('gpt2')

with model.forward(remote=True) as runner:

    with runner.invoke("testing"):
        data = model.transformer.h[0].output.save()


print(data.value)


