
import nnsight

model = nnsight.LanguageModel('gpt2')

for i in range(3):

    with model.forward(remote=True) as runner:

        with runner.invoke(["apple"] *  50):
            model.transformer.h[-1].output[0][:] += 1

            hidden_states = model.transformer.h[-1].output[0].sum().save()


    print(hidden_states.value)
