# deploy a Qwen2.5-0.5B-Instruct with 1 replica
ndif deploy Qwen/Qwen2.5-0.5B-Instruct --replicas 4 --ray-address ray://localhost:10001
# deploy a gpt2 with 1 replica
ndif deploy openai-community/gpt2 --replicas 5 --ray-address ray://localhost:10001 --broker-url redis://localhost:6379/
# scale to 4 replicas
ndif scale Qwen/Qwen2.5-0.5B-Instruct --replicas 4 --ray-address ray://localhost:10001
# evict the model with a replica id
ndif evict Qwen/Qwen2.5-0.5B-Instruct --replica-id <replica_id> --ray-address ray://localhost:10001
# restart the model
ndif restart Qwen/Qwen2.5-0.5B-Instruct --ray-address ray://localhost:10001
# evict all models
ndif evict --all --ray-address ray://localhost:10001 --broker-url redis://localhost:6379/
# restart all models
ndif restart --all --ray-address ray://localhost:10001
# get ndif queue status
ndif queue --watch --broker-url redis://localhost:6379/
# get ndif status
ndif status --ray-address ray://localhost:10001