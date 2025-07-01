import asyncio
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Any, List, Optional, Generic, TypeVar
from .status import CoordinatorStatus
from ..processing.status import ProcessorStatus

# Generic types for coordinators
T = TypeVar('T')  # Task type
P = TypeVar('P')  # Processor type

class Coordinator(ABC, Generic[T, P]):
    """
    Abstract base class for coordinators that manage processors.
    
    This class defines the core functionality for coordinating tasks across
    multiple processors, without making assumptions about the specific backend
    or implementation. Subclasses can implement specific backend integrations.
    """
    
    def __init__(self, tick_interval: float = 1.0, max_retries: int = 3, max_consecutive_errors: int = 5):
        self.active_processors: Dict[str, P] = {}
        self.inactive_processors: Dict[str, P] = {}
        self._async_task = None
        self.max_retries = max_retries
        self.tick_interval = tick_interval
        self.tick_count = 0
        self._error_count = 0
        self._max_consecutive_errors = max_consecutive_errors


    @property
    def running(self):
        """
        Check if the coordinator asyncio event loop is running.
        """
        if self._async_task:
            if not self._async_task.done():
                return True
        return False


    @property
    def status(self):
        """
        The status of the coordinator.
        """
        if not self.running:
            return CoordinatorStatus.STOPPED
        elif self._error_count >= self._max_consecutive_errors:
            return CoordinatorStatus.ERROR
        else:
            return CoordinatorStatus.RUNNING


    def get_state(self) -> Dict[str, Any]:
        """
        Get the state of the coordinator.
        
        Returns:
            Dictionary containing coordinator state information
        """
        return {
            "status": self.status,
            "active_processors": [processor.get_state() for processor in self.active_processors.values()],
            "inactive_processors": [processor.get_state() for processor in self.inactive_processors.values()],
            "error_count": self._error_count,
            "total_processors": len(self.active_processors) + len(self.inactive_processors),
            "tick_count": self.tick_count,
            "datetime": datetime.now()
        }


    async def start(self):
        """Start the coordinator."""
        if self.running:
            self._log_warning("Coordinator is already running")
            return
        
        try:
            self._error_count = 0
            self._async_task = asyncio.create_task(self._run_event_loop())
            self._log_info("Coordinator started successfully")
        except Exception as e:
            self._async_task = None
            self._log_error(f"Failed to start coordinator: {e}")
            raise


    async def stop(self):
        """Stop the coordinator."""
        if not self.running:
            self._log_warning("Coordinator is not running")
            return
        
        try:
            # Cancel monitoring task
            if self._async_task:
                self._async_task.cancel()
                try:
                    await self._async_task
                except asyncio.CancelledError:
                    # Expected outcome of calling `.cancel()`
                    pass
            
            # Clean up all processors
            await self._cleanup_all_processors()
            
            self._log_info("Coordinator stopped successfully")
        except Exception as e:
            self._log_error(f"Error stopping coordinator: {e}")
            raise


    def evict_processor(self, processor_key : str, *args, **kwargs) -> bool:
        """Evict the procesor for a given key. Returns True if the processor no longer exists in an active state after the operation."""
        
        if processor_key in self.active_processors:
            processor = self.active_processors[processor_key]
            evicted = self._evict(processor, *args, **kwargs)
            self.active_processors.pop(processor_key)
            self.inactive_processors[processor_key] = processor

        elif processor_key in self.inactive_processors:
            processor = self.inactive_processors[processor_key]
            evicted = self._evict(processor, *args, **kwargs)

        else:
            self._log_warning(f"Warning: An eviction attempt was made for a processor which does not exist: {processor_key}")
            evicted = True

        return evicted


    def _process_lifecycle_tick(self):
        """
        Advance the lifecycle of all active processors.
        """
        processors_to_deactivate = []
        processors_to_deploy = []
        failed_processors = []
        
        
        for processor_key, processor in self.active_processors.items():
            try:
                # Modify state (except for actions which need to be done in batch and background tasks)
                processor.advance_lifecycle()
                processor_status = processor.status

                if processor_status == ProcessorStatus.UNINITIALIZED: 
                    processors_to_deploy.append(processor)
                
                elif processor_status == ProcessorStatus.INACTIVE: 
                    processor._app_handle = None
                    processors_to_deactivate.append(processor_key)

                elif processor_status in [ProcessorStatus.TERMINATED, ProcessorStatus.UNAVAILABLE]:
                    failed_processors.append(processor)

                elif processor_status == ProcessorStatus.PROVISIONING:
                    
                    update_wait = float(os.environ.get("_COORDINATOR_UPDATE_INTERVAL", 1))
                    ticks_per_update = max(1, int(update_wait // self.tick_interval))
                    if self.tick_count % ticks_per_update == 0:
                        try:
                            processor.notify_pending_task()
                        except Exception as e:
                            self._log_error(f"Failed to notify_pending_tasks")
                    
            except Exception as e:
                self._log_error(f"Error advancing lifecycle for processor {processor_key}: {e}")
                processors_to_deactivate.append(processor_key)
        
        # Deploy processors        
        self._deploy(processors_to_deploy)

        # Move processors to inactive status
        for processor_key in processors_to_deactivate:
            self._deactivate_processor(processor_key)

        # Clean up failed processors
        for processor in failed_processors:
            self._handle_processor_failure(processor)


    def _deactivate_processor(self, processor_key: str):
        """
        Move a processor from active to inactive state.
        
        Args:
            processor_key: The key of the processor to deactivate
        """
        try:
            if processor_key in self.active_processors:
                processor = self.active_processors.pop(processor_key)
                if processor_key in self.inactive_processors:
                    self._log_warning(f"Processor {processor_key} is already in inactive state. This should not happen.")
                self.inactive_processors[processor_key] = processor
                self._log_info(f"Deactivated processor {processor_key}")
        except Exception as e:
            self._log_error(f"Error deactivating processor {processor_key}: {e}")


    async def _cleanup_all_processors(self):
        """
        Clean up all processors during shutdown.
        """
        try:
            # Clear all processors
            self.active_processors.clear()
            self.inactive_processors.clear()
            self._log_info("Cleaned up all processors")
        except Exception as e:
            self._log_error(f"Error during final cleanup: {e}")


    async def _run_event_loop(self):
        """
        Run the coordinator's event loop.
        """
        while self.running:
            try:
                await asyncio.sleep(self.tick_interval)
                self._process_lifecycle_tick()
                self.tick_count += 1
                self._error_count = 0  # Reset error count on successful iteration
            except asyncio.CancelledError:
                self._log_info("Event loop cancelled")
                break
            except Exception as e:
                self._error_count += 1
                self._log_error(f"Error in event loop (attempt {self._error_count}): {e}")
                
                if self._error_count >= self._max_consecutive_errors:
                    self._log_error(f"Too many consecutive errors ({self._error_count}), stopping coordinator")
                    await self.stop()
                    break
    

    @abstractmethod
    async def route_request(self, request: T) -> bool:
        """
        Abstract method to route a request to the appropriate processor.
        
        Args:
            request: The request to route
            
        Returns:
            True if routing was successful, False otherwise
        """
        pass


    @abstractmethod
    def _handle_processor_failure(self, processor: P):
        """
        Abstract method to help determine what to do with a failed procesor.
        """
        pass


    @abstractmethod
    def _create_processor(self, processor_key: str) -> P:
        """
        Abstract method to create a new processor.
        
        Args:
            processor_key: The key for the processor
            
        Returns:
            The created processor
        """
        pass


    @abstractmethod
    def _deploy(processor_keys : List[str]) -> Any:
        pass


    @abstractmethod
    def _evict(self, processor : P, *args, **kwargs) -> bool:
        """Abstract method which defines the operations which take place to evict a processor."""
        pass

    
    # Logging methods - subclasses can override these to use their own logging
    def _log_debug(self, message: str):
        """Log a debug message."""
        print(f"[DEBUG] Coordinator: {message}")


    def _log_info(self, message: str):
        """Log an info message."""
        print(f"[INFO] Coordinator: {message}")


    def _log_warning(self, message: str):
        """Log a warning message."""
        print(f"[WARNING] Coordinator: {message}")


    def _log_error(self, message: str):
        """Log an error message."""
        print(f"[ERROR] Coordinator: {message}")