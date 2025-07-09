import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger("ndif")

"""Mixins for coordination classes."""

class ProcessorStatusMixin:
    """Mixin to provide processor status capabilities to classes."""

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
            logger.error(f"Error getting processor status for {processor_key}: {e}")
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
            logger.error(f"Error getting all processors: {e}")
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