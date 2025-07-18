import asyncio
import logging
import logging
import multiprocessing
import os
import threading
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Generic, List, Optional, TypeVar

from ..processing.status import ProcessorStatus
from .status import CoordinatorStatus

# Generic types for coordinators
T = TypeVar("T")  # Task type
P = TypeVar("P")  # Processor type

logger = logging.getLogger("ndif")

class Coordinator(ABC, Generic[T, P]):
    """
    Abstract base class for coordinators that manage processors.

    This class defines the core functionality for coordinating tasks across
    multiple processors, without making assumptions about the specific backend
    or implementation. Subclasses can implement specific backend integrations.
    """

    def __init__(
        self,
        tick_interval: float = 1.0,
        max_retries: int = 3,
        max_consecutive_errors: int = 5,
    ):
        self.active_processors: Dict[str, P] = {}
        self.inactive_processors: Dict[str, P] = {}
        self.max_retries = max_retries
        self.tick_interval = tick_interval
        self.tick_count = 0
        self.running = False
        self._thread = None
        self._error_count = 0
        self._max_consecutive_errors = max_consecutive_errors

        # Attributes accessible from outside of the coordinator thread (e.g. by the Queue app)
        manager = multiprocessing.Manager()
        self._in_tick_flag = manager.Value('b', False)
        self._sleep_event = manager.Event()
        
    @property
    def in_tick(self) -> bool:
        """Returns True if currently processing a tick, False otherwise."""
        return self._in_tick_flag.value
    
    @in_tick.setter
    def in_tick(self, value: bool):
        """Sets whether we're currently processing a tick."""
        self._in_tick_flag.value = value
    
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
            "active_processors": [
                processor.get_state() for processor in self.active_processors.values()
            ],
            "inactive_processors": [
                processor.get_state() for processor in self.inactive_processors.values()
            ],
            "error_count": self._error_count,
            "total_processors": len(self.active_processors)
            + len(self.inactive_processors),
            "tick_count": self.tick_count,
            "datetime": datetime.now(),
        }

    def start(self):
        """Start the coordinator."""
        if self.running:
            logger.warning("Coordinator is already running")
            return

        try:
            self._error_count = 0
            self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
            self.running = True
            self._thread.start()
            
            logger.info("Coordinator started successfully")
        except Exception as e:
            self._thread = None
            logger.exception(f"Failed to start coordinator: {e}")
            raise

    def stop(self):
        """Stop the coordinator."""
        if not self.running:
            logger.warning("Coordinator is not running")
            return

        try:

            # Clean up all processors
            self._cleanup_all_processors()
            self.running = False
            logger.info("Coordinator stopped successfully")
        except Exception as e:
            logger.exception(f"Error stopping coordinator: {e}")
            raise

    def evict_processor(
        self, processor_key: str, reason: Optional[str] = None, *args, **kwargs
    ) -> bool:
        """Evict the procesor for a given key. Returns True if the processor no longer exists in an active state after the operation."""

        if processor_key in self.active_processors:
            processor = self.active_processors[processor_key]
            evicted = self._evict(processor, reason=reason, *args, **kwargs)
            self.active_processors.pop(processor_key)
            self.inactive_processors[processor_key] = processor

        elif processor_key in self.inactive_processors:
            processor = self.inactive_processors[processor_key]
            evicted = self._evict(processor, reason=reason, *args, **kwargs)

        else:
            logger.warning(
                f"Warning: An eviction attempt was made for a processor which does not exist: {processor_key}"
            )
            evicted = True

        return evicted

    def _interrupt_sleep(self):
        """Interrupts the current sleep in the event loop."""
        self._sleep_event.set()

    @contextmanager
    def _tick_context(self):
        """Context manager to handle tick state and ensure cleanup."""
        self.in_tick = True
        try:
            yield
        finally:
            self.in_tick = False

    def _run_event_loop(self):
        """
        Run the coordinator's event loop.
        """
        while self.running:
            try:
                # Clear the event before waiting (in case it was set previously)
                self._sleep_event.clear()
                
                # Sleep for tick_interval or until interrupted
                interrupted = self._sleep_event.wait(self.tick_interval)
                
                if interrupted:
                    logger.debug("Sleep interrupted, processing tick immediately")
                
                with self._tick_context():
                    self._process_lifecycle_tick()
                    self.tick_count += 1
                    self._error_count = 0  # Reset error count on successful iteration
                    
            except asyncio.CancelledError:
                logger.info("Event loop cancelled")
                break
            except Exception as e:
                self._error_count += 1
                logger.exception(
                    f"Error in event loop (attempt {self._error_count}): {e}"
                )

                if self._error_count >= self._max_consecutive_errors:
                    logger.exception(
                        f"Too many consecutive errors ({self._error_count}), stopping coordinator"
                    )
                    self.stop()
                    break


    def _process_lifecycle_tick(self):
        """
        Advance the lifecycle of all active processors.
        """
        processors_to_deactivate = []
        processors_to_deploy = []
        failed_processors = []

        #logger.debug(f"[COORDINATOR] Processing lifecycle tick #{self.tick_count} with {len(self.active_processors)} active processors")

        for processor_key, processor in self.active_processors.items():
            try:
                # Modify state (except for actions which need to be done in batch and background tasks)
                processor.advance_lifecycle()
                processor_status = processor.status

                if processor_status == ProcessorStatus.UNINITIALIZED:
                    logger.info(f"[COORDINATOR] Processor {processor_key} needs deployment")
                    processors_to_deploy.append(processor)

                elif processor_status == ProcessorStatus.INACTIVE:
                    logger.info(f"[COORDINATOR] Processor {processor_key} became inactive")
                    processor._app_handle = None
                    processors_to_deactivate.append(processor_key)

                elif processor_status in [
                    ProcessorStatus.TERMINATED,
                    ProcessorStatus.UNAVAILABLE,
                ]:
                    logger.warning(f"[COORDINATOR] Processor {processor_key} failed with status: {processor_status}")
                    failed_processors.append(processor)

                elif processor_status == ProcessorStatus.PROVISIONING:

                    update_wait = float(
                        os.environ.get("_COORDINATOR_UPDATE_INTERVAL", 1)
                    )
                    ticks_per_update = max(1, int(update_wait // self.tick_interval))
                    if self.tick_count % ticks_per_update == 0:
                        try:
                            processor.notify_pending_task()
                        except Exception as e:
                            logger.exception(f"Failed to notify pending tasks: {e}")

            except Exception as e:
                logger.exception(
                    f"[COORDINATOR] Error advancing lifecycle for processor {processor_key}: {e}"
                )
                processors_to_deactivate.append(processor_key)

        # Deploy processors
        if processors_to_deploy:
            logger.info(f"[COORDINATOR] Deploying {len(processors_to_deploy)} processors")
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
                    logger.warning(
                        f"Processor {processor_key} is already in inactive state. This should not happen."
                    )
                self.inactive_processors[processor_key] = processor
                logger.info(f"[COORDINATOR] Deactivated processor {processor_key}")
        except Exception as e:
            logger.exception(f"[COORDINATOR] Error deactivating processor {processor_key}: {e}")

    def _cleanup_all_processors(self):
        """
        Clean up all processors during shutdown.
        """
        try:
            # Clear all processors
            self.active_processors.clear()
            self.inactive_processors.clear()
            logger.info("[COORDINATOR] Cleaned up all processors")
        except Exception as e:
            logger.exception(f"[COORDINATOR] Error during final cleanup: {e}")


    @abstractmethod
    def route_request(self, request: T) -> bool:
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
    def _deploy(self, processors: List[P]) -> Any:
        pass

    @abstractmethod
    def _evict(self, processor: P, *args, **kwargs) -> bool:
        """Abstract method which defines the operations which take place to evict a processor."""
        pass