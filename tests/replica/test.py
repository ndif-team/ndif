from nnsight import LanguageModel, CONFIG
import time
# model = LanguageModel("Qwen/Qwen2.5-0.5B-Instruct")
model = LanguageModel("openai-community/gpt2")
# model = LanguageModel("EleutherAI/pythia-1b")
CONFIG.API.HOST = "http://localhost:5001"

# Run the trace
time_start = time.time()
with model.trace("tell me a joke with 10000 words", remote=True, max_new_tokens=1000) as tracer:
    hs = model.transformer.h[-1].output[0].save()
    output = tracer.result.save()

# Get the request ID from the backend after trace completes
request_id = tracer.backend.job_id
time_end = time.time()
print("Time taken (milliseconds):", (time_end - time_start) * 1000)
print("Hidden States Shape:", hs.shape)
print("Output:", output)
print("Request ID:", request_id)
