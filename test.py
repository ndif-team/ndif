
import nnsight

model = nnsight.LanguageModel('EleutherAI/gpt-j-6b')

with model.forward(remote=True, validate=False) as runner:

    with runner.invoke("testing", scan=False):
        data = model.transformer.h[0].output.save()

print('ayy')
with model.forward(remote=True, validate=False) as runner:

    with runner.invoke("testing", scan=False):
        data = model.transformer.h[0].output.save()
print('ayy')
with model.forward(remote=True, validate=False) as runner:

    with runner.invoke("testing", scan=False):
        data = model.transformer.h[0].output.save()

print('ayy')