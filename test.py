
import nnsight

model = nnsight.LanguageModel('EleutherAI/gpt-j-6b')

for i in range(3):

    with model.forward(remote=True, validate=False) as runner:

        with runner.invoke(["apple"] *  50, scan=False):
            model.transformer.h[-1].output[0][:] += 1

            hidden_states = model.transformer.h[-1].output.save()


    print(hidden_states.value)
