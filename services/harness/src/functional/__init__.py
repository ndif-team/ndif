import os
from nnsight import CONFIG
from distutils.util import strtobool

CONFIG.API.HOST = os.getenv("API_HOST")
CONFIG.API.SSL = bool(strtobool(os.getenv("API_SSL")))
CONFIG.API.APIKEY = os.getenv("API_KEY")
CONFIG.APP.REMOTE_LOGGING = bool(strtobool(os.getenv("APP_REMOTE_LOGGING")))
