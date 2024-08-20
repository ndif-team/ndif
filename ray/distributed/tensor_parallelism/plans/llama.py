from torch.distributed._tensor import Replicate, Shard
from torch.distributed.tensor.parallel import (
    ColwiseParallel,
    PrepareModuleInput,
    RowwiseParallel,
    SequenceParallel,
)



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
    "model.layers.*self_attn.q_proj": ColwiseParallel(),
    "model.layers.*self_attn.k_proj": ColwiseParallel(),
    "model.layers.*self_attn.v_proj": ColwiseParallel(),
    "model.layers.*self_attn.o_proj": RowwiseParallel(),
    "model.layers.*mlp.gate_proj": ColwiseParallel(),
    "model.layers.*mlp.up_proj": ColwiseParallel(),
    "model.layers.*mlp.down_proj": RowwiseParallel(),
}
