import datasets
import torch

from transformers import AutoModelForCausalLM, AutoTokenizer
from transformers.generation import GenerationConfig



MODEL_ID="meta-llama/Llama-3.3-70B-Instruct"
NUM_SAMPLES=10

model = AutoModelForCausalLM.from_pretrained(
    MODEL_ID,
    attn_implementation="paged|sdpa",
    device_map="auto",  # if you need: cuda
    dtype=torch.bfloat16,
)
tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, padding_side="left")

# prepare a batch of inputs
dataset = datasets.load_dataset("openai/gsm8k", "socratic", split="test")
dataset = dataset.select(range(NUM_SAMPLES))
tokenized_datasets = dataset.map(lambda x: tokenizer(x["question"]), batched=True)
simple_batch_inputs = [item["input_ids"] for item in tokenized_datasets]

generation_config = GenerationConfig(
    max_new_tokens=128,
    eos_token_id=tokenizer.eos_token_id,
    pad_token_id=tokenizer.pad_token_id,
    do_sample=False,
    max_batch_tokens=512,  # max number of tokens in a batch, this is just a default value you should tune based on your hardware
)

manager = model.init_continuous_batching(generation_config=generation_config)

# start the background thread
manager.start()

# this is for demonstration purposes only, in practice this is most useful to do concurrently
for i, input in enumerate(simple_batch_inputs):
    request_id = manager.add_request(input_ids=input, request_id=f"request_{i}")  # if you do not specify a request_id, one will be generated for you

# Can be done in another thread
for id, request in manager.get_result():
    generated_text = tokenizer.decode(request.generated_tokens, skip_special_tokens=True)
    print(f"Request {id} output: {generated_text}")
