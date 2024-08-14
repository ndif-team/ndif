import random
from string import printable

def gen_rand_str(n_chars : int) -> str:
  '''Helper function which genrates a random string of length `n_chars`'''
  return ''.join([random.choice(printable) for _ in range(n_chars)])
