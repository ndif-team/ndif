import random
from string import printable

def gen_rand_str(n_chars : int) -> str:
  '''Helper function which genrates a random string of length `n_chars`'''
  return ''.join([random.choice(printable) for _ in range(n_chars)])

def get_num_layers(config: "AutoConfig") -> int:
    # Try common config attributes for the number of layers
    if hasattr(config, 'num_hidden_layers'):   # For models like BERT, GPT-2
        return config.num_hidden_layers
    elif hasattr(config, 'n_layer'):           # For models like GPT
        return config.n_layer
    elif hasattr(config, 'encoder_layers'):    # For models like BART, T5
        return config.encoder_layers
    elif hasattr(config, 'num_layers'):        # For some other models
        return config.num_layers
    else:
        raise ValueError(f"Unknown number of layers for model {model_name}")