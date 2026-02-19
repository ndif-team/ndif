# should disable fraction first

# deploy 8 models to take 8 gpus
# evict gpt-neo-125m to free up a gpu
# deploy gpt-neo-1.3B to take the freed up gpu
# evict Qwen/Qwen2.5-1.5B-Instruct to free up another gpu
# deploy gpt-neo-125m again to take the freed up gpu

ndif deploy openai-community/gpt2 --ray-address ray://localhost:10001 --broker-url redis://localhost:6379
ndif deploy EleutherAI/gpt-neo-125m --ray-address ray://localhost:10001 --broker-url redis://localhost:6379
ndif deploy Qwen/Qwen2.5-0.5B --ray-address ray://localhost:10001 --broker-url redis://localhost:6379
ndif deploy Qwen/Qwen2.5-0.5B-Instruct --ray-address ray://localhost:10001 --broker-url redis://localhost:6379
ndif deploy Qwen/Qwen2.5-1.5B --ray-address ray://localhost:10001 --broker-url redis://localhost:6379
ndif deploy Qwen/Qwen2.5-1.5B-Instruct --ray-address ray://localhost:10001 --broker-url redis://localhost:6379
ndif deploy Qwen/Qwen2.5-3B --ray-address ray://localhost:10001 --broker-url redis://localhost:6379
ndif deploy Qwen/Qwen2.5-3B-Instruct --ray-address ray://localhost:10001 --broker-url redis://localhost:6379
ndif evict  EleutherAI/gpt-neo-125m --ray-address ray://localhost:10001 --broker-url redis://localhost:6379
ndif deploy  EleutherAI/gpt-neo-1.3B --ray-address ray://localhost:10001 --broker-url redis://localhost:6379
ndif evict Qwen/Qwen2.5-1.5B-Instruct --ray-address ray://localhost:10001 --broker-url redis://localhost:6379
ndif deploy EleutherAI/gpt-neo-125m --ray-address ray://localhost:10001 --broker-url redis://localhost:6379

ndif status --verbose --ray-address ray://localhost:10001 
