"""NDIF sandbox security.

Public API:
    Protector                        – context manager that activates the sandbox
    WHITELISTED_MODULES              – modules allowed during execution
    WHITELISTED_MODULES_DESERIALIZATION – modules allowed during deserialization
"""

from .protector import Protector
from .whitelist import WHITELISTED_MODULES, WHITELISTED_MODULES_DESERIALIZATION
