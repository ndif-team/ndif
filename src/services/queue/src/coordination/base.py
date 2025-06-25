import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Any, List, Optional, Generic, TypeVar
from .state import CoordinatorState

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
            return CoordinatorState.STOPPED
        elif self._error_count >= self._max_consecutive_errors:
            return CoordinatorState.ERROR
        else:
            return CoordinatorState.RUNNING

    def state(self) -> Dict[str, Any]:
        """
        Get the state of the coordinator.
        
        Returns:
            Dictionary containing coordinator state information
        """
        return {
            "status": self.status,
            "active_processors": [processor.state() for processor in self.active_processors.values()],
            "inactive_processors": [processor.state() for processor in self.inactive_processors.values()],
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
            self._async_task = asyncio.create_task(self._monitor_processors())
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
    def _should_deactivate_processor(self, processor: P) -> bool:
        """
        Abstract method to determine if a processor should be deactivated.
        
        Args:
            processor: The processor to check
            
        Returns:
            True if the processor should be deactivated, False otherwise
        """
        pass

    @abstractmethod
    def _should_update_processor(self, processor: P) -> bool:
        """
        Abstract method to determine if a processor should be updated.
        """
        pass

    @abstractmethod
    def _should_deploy_processor(self, processor: P) -> bool:
        """
        Abstract method to determine if a processor should be deployed.
        """
        pass

    @abstractmethod
    def _processor_failed(self, processor: P) -> bool:
        """
        Abstract method to determine if a processor has failed and needs resolving.
        """
        pass

    @abstractmethod
    def handle_processor_failure(self, processor: P):
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

    def _advance_processor_lifecycles(self):
        """
        Advance the lifecycle of all active processors.
        """
        processors_to_deactivate = []
        processors_to_update = []
        processors_to_deploy = []
        failed_processors = []
        
        
        for processor_key, processor in self.active_processors.items():
            try:
                # Modify state (except for actions which need to be done in batch and background tasks)
                processor.advance_lifecycle()
                
                if self._should_deploy_processor(processor):
                    processors_to_deploy.append(processor)

                elif self._should_deactivate_processor(processor):
                    processors_to_deactivate.append(processor_key)

                elif self._should_update_processor(processor):
                    processors_to_update.append(processor_key)

                elif self._processor_failed(processor):
                    failed_processors.append(processor)
                    
            except Exception as e:
                self._log_error(f"Error advancing lifecycle for processor {processor_key}: {e}")
                processors_to_deactivate.append(processor_key)
        
        # Deploy processors        
        self._deploy(processors_to_deploy)

        # Move processors to inactive state
        for processor_key in processors_to_deactivate:
            self._deactivate_processor(processor_key)

        # Clean up failed processors
        for processor in failed_processors:
            self.handle_processor_failure(processor)

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

    async def _monitor_processors(self):
        """
        Monitor and advance processor lifecycles.
        """
        while self.running:
            try:
                await asyncio.sleep(self.tick_interval)
                self._advance_processor_lifecycles()
                self.tick_count += 1
                self._error_count = 0  # Reset error count on successful iteration
            except asyncio.CancelledError:
                self._log_info("Processor monitoring cancelled")
                break
            except Exception as e:
                self._error_count += 1
                self._log_error(f"Error in processor monitoring (attempt {self._error_count}): {e}")
                
                if self._error_count >= self._max_consecutive_errors:
                    self._log_error(f"Too many consecutive errors ({self._error_count}), stopping coordinator")
                    await self.stop()
                    break

    def get_processor_status(self, processor_key: str) -> Optional[Dict[str, Any]]:
        """
        Get the status of a specific processor.
        
        Args:
            processor_key: The key of the processor
            
        Returns:
            Dictionary containing processor status, or None if not found
        """
        try:
            if processor_key in self.active_processors:
                return {
                    "processor_key": processor_key,
                    "status": "active",
                    "processor_state": self.active_processors[processor_key].state()
                }
            elif processor_key in self.inactive_processors:
                return {
                    "processor_key": processor_key,
                    "status": "inactive",
                    "processor_state": self.inactive_processors[processor_key].state()
                }
            else:
                return None
        except Exception as e:
            self._log_error(f"Error getting processor status for {processor_key}: {e}")
            return None

    def get_all_processors(self) -> List[Dict[str, Any]]:
        """
        Get status of all processors.
        
        Returns:
            List of dictionaries containing processor status information
        """
        try:
            processors = []
            
            for processor_key, processor in self.active_processors.items():
                processors.append({
                    "processor_key": processor_key,
                    "status": "active",
                    "processor_state": processor.state()
                })
            
            for processor_key, processor in self.inactive_processors.items():
                processors.append({
                    "processor_key": processor_key,
                    "status": "inactive",
                    "processor_state": processor.state()
                })
            
            return processors
        except Exception as e:
            self._log_error(f"Error getting all processors: {e}")
            return []

    def get_active_processor_count(self) -> int:
        """
        Get the number of active processors.
        
        Returns:
            Number of active processors
        """
        return len(self.active_processors)

    def get_inactive_processor_count(self) -> int:
        """
        Get the number of inactive processors.
        
        Returns:
            Number of inactive processors
        """
        return len(self.inactive_processors)

    def get_total_processor_count(self) -> int:
        """
        Get the total number of processors.
        
        Returns:
            Total number of processors
        """
        return len(self.active_processors) + len(self.inactive_processors)

    def is_processor_active(self, processor_key: str) -> bool:
        """
        Check if a processor is active.
        
        Args:
            processor_key: The key of the processor
            
        Returns:
            True if the processor is active, False otherwise
        """
        return processor_key in self.active_processors

    def is_processor_inactive(self, processor_key: str) -> bool:
        """
        Check if a processor is inactive.
        
        Args:
            processor_key: The key of the processor
            
        Returns:
            True if the processor is inactive, False otherwise
        """
        return processor_key in self.inactive_processors

    def has_processor(self, processor_key: str) -> bool:
        """
        Check if a processor exists (active or inactive).
        
        Args:
            processor_key: The key of the processor
            
        Returns:
            True if the processor exists, False otherwise
        """
        return processor_key in self.active_processors or processor_key in self.inactive_processors

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
