import os
import logging
import threading
import time
from functools import wraps
from typing import Callable, Type

logger = logging.getLogger("ndif")


class Provider:
    
    max_retries: int
    retry_interval: float
    
    watchdog = None    
    
    @classmethod
    def from_env(cls) -> None:
        cls.max_retries = int(os.environ.get("PROVIDER_MAX_RETRIES", 3))
        cls.retry_interval = float(os.environ.get("PROVIDER_RETRY_INTERVAL_S", 5))
        
    @classmethod
    def to_env(cls) -> dict:
        return {
            "PROVIDER_MAX_RETRIES": cls.max_retries,
            "PROVIDER_RETRY_INTERVAL_S": cls.retry_interval,
        }
        
    @classmethod
    def connect(cls) -> None:
        pass
    
    @classmethod
    def disconnect(cls) -> None:
        pass
    
    @classmethod
    def connected(cls) -> bool:
        return True
    
    @classmethod
    def reset(cls) -> None:
        pass
    
    @classmethod
    def watch(cls) -> None:
        if cls.watchdog is None:
            cls.watchdog = threading.Thread(target=cls.watchdog_loop)
            cls.watchdog.start()
            
    @classmethod
    def stop_watchdog(cls) -> None:
        cls.watchdog = None
    
    @classmethod
    def watchdog_loop(cls) -> None:
        while cls.watchdog is not None:
            try:
                if not cls.connected():
                    logger.warning(f"Provider {cls.__name__} is not connected, attempting to reconnect...")
                    cls.reset()
                    cls.connect()
            except Exception as e:
                logger.exception(f"Error in watchdog loop for provider {cls.__name__}: {e}")
            
            time.sleep(cls.retry_interval)
            
    def __enter__(self):
        if not self.connected():
            self.connect()
        return self
    
    def __exit__(self, exc_type, exc_value, traceback):
        pass
    
    
def retry(fn: Callable) -> Callable:
    
    @wraps(fn)
    def inner(cls:Type[Provider], *args, **kwargs):
        
        exception = None

        logger.debug(f"Attempting to call {cls.__name__}.{fn.__name__}() with {cls.max_retries} retries...")
        
        for _ in range(cls.max_retries):
            try:
                if not cls.connected() and fn.__name__ != 'connect':
                    cls.reset()
                    cls.connect()
                return fn(cls,*args, **kwargs)
            except Exception as e:
                exception = e
                time.sleep(cls.retry_interval)
                
        if exception is not None:
            raise exception
    
    return inner
