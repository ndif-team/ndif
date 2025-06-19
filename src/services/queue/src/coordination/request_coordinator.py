import asyncio
import os
import time
import ray
from ray import serve
from typing import Dict, Optional, List
from slugify import slugify
from ..logging import load_logger
from ..schema import BackendRequestModel
from ..processing.request_processor import RequestProcessor
from ..processing.state import ProcessorState
from .base import Coordinator

logger = load_logger(service_name="Queue", logger_name="RequestCoordinator")

class RequestCoordinator(Coordinator[BackendRequestModel, RequestProcessor]):
    """
    Coordinates requests between the queue and the model deployments using Ray backend.
    """

    def __init__(self, tick_interval: float = 1.0, max_retries: int = 3):
        super().__init__(tick_interval, max_retries)

    async def route_request(self, request: BackendRequestModel) -> bool:
        """Route request to appropriate processor."""
        try:
            # Validate request
            if not request or not request.model_key:
                self._log_error("Invalid request: missing model_key")
                return False
            
            model_key = f"Model:{slugify(request.model_key)}"
            
            # Try to route to existing active processor
            if model_key in self.active_processors:
                processor = self.active_processors[model_key]
                success = processor.enqueue(request)
                if success:
                    self._log_debug(f"Request {request.id} routed to active processor {model_key}")
                    return True
                else:
                    self._log_error(f"Failed to enqueue request {request.id} to active processor {model_key}")
                    return False

            # Try to route to inactive processor
            elif model_key in self.inactive_processors:
                processor = self.inactive_processors[model_key]
                success = processor.enqueue(request)
                if success:
                    # Move processor to active state
                    self.active_processors[model_key] = processor
                    del self.inactive_processors[model_key]
                    self._log_info(f"Activated processor {model_key} and routed request {request.id}")
                    return True
                else:
                    self._log_error(f"Failed to enqueue request {request.id} to inactive processor {model_key}")
                    return False

            # Create new processor
            else:
                try:
                    processor = self._create_processor(model_key)
                    success = processor.enqueue(request)
                    if success:
                        self.active_processors[model_key] = processor
                        self._log_info(f"Created new processor {model_key} and routed request {request.id}")
                        return True
                    else:
                        self._log_error(f"Failed to enqueue request {request.id} to new processor {model_key}")
                        return False
                except Exception as e:
                    self._log_error(f"Failed to create processor {model_key}: {e}")
                    return False
                    
        except Exception as e:
            self._log_error(f"Error routing request {request.id if request else 'unknown'}: {e}")
            return False

    def _should_deactivate_processor(self, processor: RequestProcessor) -> bool:
        """Determine if a processor should be deactivated."""
        try:
            # Deactivate if processor is inactive
            if processor.status == ProcessorState.INACTIVE:
                return True
            
            return False
        except Exception as e:
            self._log_error(f"Error checking if processor should be deactivated: {e}")
            return True  # Deactivate on error

    def _create_processor(self, processor_key: str) -> RequestProcessor:
        """Create a new RequestProcessor."""
        return RequestProcessor(processor_key, self.max_retries)

    def get_processor_status(self, model_key: str) -> Optional[Dict]:
        """Get the status of a specific processor."""
        try:
            slugified_key = f"Model:{slugify(model_key)}"
            return super().get_processor_status(slugified_key)
        except Exception as e:
            self._log_error(f"Error getting processor status for {model_key}: {e}")
            return None

    def get_all_processors(self) -> List[Dict]:
        """Get status of all processors."""
        try:
            return super().get_all_processors()
        except Exception as e:
            self._log_error(f"Error getting all processors: {e}")
            return []

    @staticmethod
    def connect_to_ray(ray_url: str):
        """Connect to Ray cluster."""
        retry_interval = int(os.environ.get("RAY_RETRY_INTERVAL_S", 5))
        while True:
            try:
                if not ray.is_initialized():
                    ray.shutdown()
                    serve.context._set_global_client(None)
                    ray.init(logging_level="error", address=ray_url)
                    logger.info("Connected to Ray cluster.")
                    return
            except Exception as e:
                logger.error(f"Failed to connect to Ray cluster: {e}")
            time.sleep(retry_interval)

    # Override logging methods to use the service logger
    def _log_debug(self, message: str):
        """Log a debug message using the service logger."""
        logger.debug(message)

    def _log_info(self, message: str):
        """Log an info message using the service logger."""
        logger.info(message)

    def _log_warning(self, message: str):
        """Log a warning message using the service logger."""
        logger.warning(message)

    def _log_error(self, message: str):
        """Log an error message using the service logger."""
        logger.error(message)
