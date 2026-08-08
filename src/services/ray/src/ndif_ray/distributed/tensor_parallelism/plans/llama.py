from torch.distributed._tensor import Replicate, Shard
from torch.distributed.tensor.parallel import (
    ColwiseParallel,
    PrepareModuleInput,
    RowwiseParallel,
    SequenceParallel,
)


def update_attention(module, mesh):

    module.num_heads = module.num_heads // mesh.size()
    module.num_key_value_heads = module.num_key_value_heads // mesh.size()


model_plans = {
    # "model.norm": SequenceParallel(),
    # "model.embed_tokens": RowwiseParallel(
    #     input_layouts=Replicate(),
    #     output_layouts=Shard(1),
    # ),
    "lm_head": ColwiseParallel(
        output_layouts=Replicate(),
        use_local_output=True,
    ),
    # "model.layers.*input_layernorm": SequenceParallel(),
    # "model.layers.*post_attention_layernorm": SequenceParallel(),
    # "model.layers.*self_attn": PrepareModuleInput(
    #     input_layouts=(Shard(1), None),
    #     desired_input_layouts=(Replicate(), None),
    # ),
    # "model.layers.*mlp": PrepareModuleInput(
    #     input_layouts=(Shard(1),),
    #     desired_input_layouts=(Replicate(),),
    # ),
    "model.layers.*self_attn.q_proj": ColwiseParallel(
        output_layouts=Replicate(),
        use_local_output=True,
    ),
    "model.layers.*self_attn.k_proj": ColwiseParallel(
        output_layouts=Replicate(),
        use_local_output=True,
    ),
    "model.layers.*self_attn.v_proj": ColwiseParallel(
        output_layouts=Replicate(),
        use_local_output=True,
    ),
    "model.layers.*self_attn.o_proj": ColwiseParallel(
        output_layouts=Replicate(),
        use_local_output=True,
    ),
    "model.layers.*mlp.gate_proj": ColwiseParallel(use_local_output=False),
    "model.layers.*mlp.up_proj": ColwiseParallel(use_local_output=False),
    "model.layers.*mlp.down_proj": RowwiseParallel(),
    # "model.layers.*self_attn": update_attention,
}
