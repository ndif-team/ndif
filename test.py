
import nnsight

model = nnsight.LanguageModel('gpt2')

for i in range(3):

    with model.forward(remote=True, validate=False) as runner:

        with runner.invoke(["testingascascacaccascascascasc"] *  50, scan=False):
            hidden_states = model.transformer.h[-1].output[0][:, -1].save()


    print(hidden_states.value)
