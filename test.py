
import nnsight

model = nnsight.LanguageModel('EleutherAI/gpt-j-6b')

with model.forward(remote=True) as runner:

    with runner.invoke("testing"):
        pass


