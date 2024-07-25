import json

import torch
from huggingface_hub import hf_hub_download
from safetensors.torch import load_file
from tqdm import tqdm


def load_hf_model_from_cache(model: torch.nn.Module, repo_id: str):

    model_index_filename = "model.safetensors.index.json"
    index_path = hf_hub_download(repo_id=repo_id, filename=model_index_filename)

    with open(index_path, "r") as f:
        index = json.load(f)

    shard_paths = sorted(set(index["weight_map"].values()))

    pbar = tqdm(shard_paths, desc="Loading shards")

    for shard_file in pbar:
        # Get path to shard
        shard_path = hf_hub_download(repo_id=repo_id, filename=shard_file)
        pbar.set_postfix({"Current shard": shard_file})

        # Get path to shard
        state_dict = load_file(shard_path, device="cpu")

        model.load_state_dict(state_dict, strict=False, assign=True)
        
        torch.cuda.empty_cache()
