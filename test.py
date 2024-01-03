
import nnsight

model = nnsight.LanguageModel('EleutherAI/gpt-j-6b')

for i in range(3):

    with model.forward(remote=True, validate=False) as runner:

        with runner.invoke(["apple"] *  1, scan=False):
            hidden_states = model.transformer.h[-1].output.save()


    print(hidden_states.value)
