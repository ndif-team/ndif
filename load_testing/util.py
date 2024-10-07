import logging
import random
from string import printable

def gen_rand_ip() -> str:
    '''Generate a random (IPv4) IP address.'''
    return ".".join(str(random.randint(0, 255)) for _ in range(4))

def gen_rand_str(n_chars : int) -> str:
  '''Helper function which genrates a random string of length `n_chars`'''
  return ''.join([random.choice(printable) for _ in range(n_chars)])

def get_api_key(config: dict) -> str:
    '''Returns an api key based on configuration settings'''
    api_config = config['custom']['api_keys']
    if api_config['random']:
        return random.choice(api_config['keys'])
    else:
        return  api_config['keys'][0]

def get_log_level(level: str):
    '''Returns the logging level corresponding to the string level name.'''
    if level.lower() == 'debug':
        return logging.DEBUG
    elif level.lower() == 'info':
        return logging.INFO
    elif level.lower() == 'warning':
        return logging.WARNING
    elif level.lower() == 'error':
        return logging.ERROR
    elif level.lower() == 'critical':
        return logging.CRITICAL
    else:
        raise ValueError(f"Unknown log level: {level}")

def get_model_key(config: dict) -> str:
    '''Returns a model key based on configuration settings'''
    model_config = config['custom']['model_keys']
    if model_config['random']:
        return random.choice(model_config['keys'])
    else:
        return  model_config['keys'][0]
        
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