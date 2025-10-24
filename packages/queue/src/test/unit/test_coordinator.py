"""
Unit tests for the base Coordinator class.
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
from typing import Any

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from src.coordination.base import Coordinator
from src.coordination.status import CoordinatorStatus
from src.processing.status import ProcessorStatus, DeploymentStatus


class MockRequest:
    """Mock request class for testing."""
    
    def __init__(self, request_id: str, processor_key: str = None):
        self.id = request_id
        self.processor_key = processor_key or f"processor-{request_id}"


class MockProcessor:
    """Mock processor class for testing."""
    
    def __init__(self, processor_id: str):
        self.id = processor_id
        self._status = ProcessorStatus.ACTIVE
        self.backend_status = DeploymentStatus.DEPLOYED
        self.queue = []
        self.dispatched_task = None
        self.deletion_queue = []
        self._has_been_terminated = False
        self.enqueue_result = True
        self.advance_lifecycle_called = 0
        self.restart_called = False
        
    @property 
    def status(self):
        return self._status
        
    def set_status(self, status):
        self._status = status
        
    def enqueue(self, request):
        if self.enqueue_result:
            self.queue.append(request)
        return self.enqueue_result
        
    def has_task(self, task_id: str) -> bool:
        if self.dispatched_task and getattr(self.dispatched_task, 'id', None) == task_id:
            return True
        for task in self.queue:
            if getattr(task, 'id', None) == task_id:
                return True
        return False
        
    def advance_lifecycle(self):
        self.advance_lifecycle_called += 1
        
    def restart(self):
        self.restart_called = True
        self.queue.clear()
        
    def respond_failure(self, description):
        pass


class MockCoordinator(Coordinator[MockRequest, MockProcessor]):
    """Concrete implementation of Coordinator for testing."""
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.backend_handle_value = None
        self.failure_messages = {}
        
    def _get_processor_key(self, request: MockRequest) -> str:
        return request.processor_key
        
    def _get_failure_message(self, processor: MockProcessor, status) -> str:
        return self.failure_messages.get(status, f"Processor {processor.id} failed with status {status}")
        
    def set_backend_handle(self, handle):
        self.backend_handle_value = handle
        
    @property
    def backend_handle(self):
        return self.backend_handle_value
        
    def _create_processor(self, processor_key: str) -> MockProcessor:
        return MockProcessor(processor_key)


class TestCoordinatorInitialization:
    """Test cases for Coordinator initialization and basic properties."""
    
    def test_status_property_stopped(self):
        """Test status property when coordinator is stopped."""
        coordinator = MockCoordinator()
        assert coordinator.status == CoordinatorStatus.STOPPED
    
    def test_status_property_error(self):
        """Test status property when error count is too high."""
        coordinator = MockCoordinator(max_consecutive_errors=2)
        coordinator._error_count = 2
        coordinator.running = True
        
        assert coordinator.status == CoordinatorStatus.ERROR
    
    def test_status_property_running(self):
        """Test status property when running normally."""
        coordinator = MockCoordinator()
        coordinator.running = True
        
        assert coordinator.status == CoordinatorStatus.RUNNING
    
    def test_get_state_empty_coordinator(self):
        """Test get_state returns correct info for empty coordinator."""
        coordinator = MockCoordinator()
        
        state = coordinator.get_state()
        
        assert state["status"] == CoordinatorStatus.STOPPED
        assert state["active_processors"] == []
        assert state["inactive_processors"] == []
        assert state["error_count"] == 0
        assert state["total_processors"] == 0
        assert state["tick_count"] == 0
        assert "datetime" in state
    
    def test_backend_handle_property(self):
        """Test backend_handle property can be set and retrieved."""
        coordinator = MockCoordinator()
        mock_handle = Mock()
        
        # Should default to None
        assert coordinator.backend_handle is None
        
        # Should be settable
        coordinator.set_backend_handle(mock_handle)
        assert coordinator.backend_handle is mock_handle


class TestCoordinatorRouteRequest:
    """Test cases for request routing functionality."""
    
    def test_route_request_to_active_processor(self):
        """Test routing request to existing active processor."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        coordinator.active_processors["test-processor"] = processor
        request = MockRequest("req-1", "test-processor")
        
        with patch.object(coordinator, '_interrupt_sleep') as mock_interrupt:
            result = coordinator.route_request(request)
            
            assert result is True
            assert len(processor.queue) == 1
            assert processor.queue[0] is request
            mock_interrupt.assert_called_once()
    
    def test_route_request_to_active_processor_enqueue_fails(self):
        """Test routing when active processor enqueue fails."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        processor.enqueue_result = False
        coordinator.active_processors["test-processor"] = processor
        request = MockRequest("req-1", "test-processor")
        
        result = coordinator.route_request(request)
        
        assert result is False
        assert len(processor.queue) == 0
    
    def test_route_request_to_inactive_processor(self):
        """Test routing request to inactive processor (should restart and activate)."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        coordinator.inactive_processors["test-processor"] = processor
        request = MockRequest("req-1", "test-processor")
        
        result = coordinator.route_request(request)
        
        assert result is True
        assert processor.restart_called is True
        assert len(processor.queue) == 1
        assert processor.queue[0] is request
        # Should move from inactive to active
        assert "test-processor" in coordinator.active_processors
        assert "test-processor" not in coordinator.inactive_processors
        assert coordinator.active_processors["test-processor"] is processor
    
    def test_route_request_to_inactive_processor_enqueue_fails(self):
        """Test routing to inactive processor when enqueue fails."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        processor.enqueue_result = False
        coordinator.inactive_processors["test-processor"] = processor
        request = MockRequest("req-1", "test-processor")
        
        result = coordinator.route_request(request)
        
        assert result is False
        assert processor.restart_called is True
        # Should stay in inactive processors
        assert "test-processor" in coordinator.inactive_processors
        assert "test-processor" not in coordinator.active_processors
    
    def test_route_request_create_new_processor(self):
        """Test routing request that requires creating new processor."""
        coordinator = MockCoordinator()
        request = MockRequest("req-1", "new-processor")
        
        result = coordinator.route_request(request)
        
        assert result is True
        assert "new-processor" in coordinator.active_processors
        processor = coordinator.active_processors["new-processor"]
        assert isinstance(processor, MockProcessor)
        assert processor.id == "new-processor"
        assert len(processor.queue) == 1
        assert processor.queue[0] is request
    
    def test_route_request_create_processor_enqueue_fails(self):
        """Test routing when new processor creation succeeds but enqueue fails."""
        coordinator = MockCoordinator()
        request = MockRequest("req-1", "new-processor")
        
        # Mock _create_processor to return processor that fails enqueue
        with patch.object(coordinator, '_create_processor') as mock_create:
            failing_processor = MockProcessor("new-processor")
            failing_processor.enqueue_result = False
            mock_create.return_value = failing_processor
            
            result = coordinator.route_request(request)
            
            assert result is False
            assert "new-processor" not in coordinator.active_processors
    
    def test_route_request_create_processor_fails(self):
        """Test routing when processor creation fails."""
        coordinator = MockCoordinator()
        request = MockRequest("req-1", "new-processor")
        
        with patch.object(coordinator, '_create_processor', side_effect=Exception("Creation failed")):
            result = coordinator.route_request(request)
            
            assert result is False
            assert "new-processor" not in coordinator.active_processors
    
    def test_route_request_invalid_request(self):
        """Test routing with invalid request."""
        coordinator = MockCoordinator()
        
        # Test with None request
        result = coordinator.route_request(None)
        assert result is False
        
        # Test with request that has no processor key
        with patch.object(coordinator, '_get_processor_key', return_value=None):
            request = MockRequest("req-1")
            result = coordinator.route_request(request)
            assert result is False
    
    def test_route_request_exception_handling(self):
        """Test route_request handles exceptions gracefully."""
        coordinator = MockCoordinator()
        request = MockRequest("req-1", "test-processor")
        
        with patch.object(coordinator, '_get_processor_key', side_effect=Exception("Key extraction failed")):
            result = coordinator.route_request(request)
            
            assert result is False


class TestCoordinatorTaskManagement:
    """Test cases for task management functionality."""
    
    def test_remove_task_found_in_processor(self):
        """Test remove_task successfully finds and marks task for deletion."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        request = MockRequest("target-task")
        processor.queue.append(request)
        coordinator.active_processors["test-processor"] = processor
        
        result = coordinator.remove_task("target-task")
        
        assert result is True
        assert "target-task" in processor.deletion_queue
    
    def test_remove_task_not_found(self):
        """Test remove_task when task is not found in any processor."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        coordinator.active_processors["test-processor"] = processor
        
        result = coordinator.remove_task("non-existent-task")
        
        assert result is False
        assert len(processor.deletion_queue) == 0
    
    def test_remove_task_multiple_processors(self):
        """Test remove_task searches through multiple processors."""
        coordinator = MockCoordinator()
        processor1 = MockProcessor("processor1")
        processor2 = MockProcessor("processor2")
        
        request = MockRequest("target-task")
        processor2.queue.append(request)  # Task is in processor2
        
        coordinator.active_processors["processor1"] = processor1
        coordinator.active_processors["processor2"] = processor2
        
        result = coordinator.remove_task("target-task")
        
        assert result is True
        assert len(processor1.deletion_queue) == 0  # Not found in processor1
        assert "target-task" in processor2.deletion_queue  # Found in processor2


class TestCoordinatorEvictionAndFailure:
    """Test cases for processor eviction and failure handling."""
    
    def test_evict_processor_from_active(self):
        """Test evicting processor from active processors."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        coordinator.active_processors["test-processor"] = processor
        
        with patch.object(coordinator, '_evict', return_value=True) as mock_evict:
            result = coordinator.evict_processor("test-processor", user_message="Test eviction")
            
            assert result is True
            mock_evict.assert_called_once_with(processor, user_message="Test eviction")
            # Should move from active to inactive
            assert "test-processor" not in coordinator.active_processors
            assert "test-processor" in coordinator.inactive_processors
            assert coordinator.inactive_processors["test-processor"] is processor
    
    def test_evict_processor_from_inactive(self):
        """Test evicting processor from inactive processors."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        coordinator.inactive_processors["test-processor"] = processor
        
        with patch.object(coordinator, '_evict', return_value=True) as mock_evict:
            result = coordinator.evict_processor("test-processor", user_message="Test eviction")
            
            assert result is True
            mock_evict.assert_called_once_with(processor, user_message="Test eviction")
            # Should remain in inactive
            assert "test-processor" in coordinator.inactive_processors
    
    def test_evict_processor_not_found(self):
        """Test evicting processor that doesn't exist."""
        coordinator = MockCoordinator()
        
        result = coordinator.evict_processor("non-existent")
        
        assert result is True  # Returns True even if not found
    
    def test_evict_implementation(self):
        """Test the concrete _evict implementation."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        
        # Set up processor with dispatched task and queued tasks
        dispatched_task = MockRequest("dispatched-1")
        queued_task1 = MockRequest("queued-1")  
        queued_task2 = MockRequest("queued-2")
        
        processor.dispatched_task = dispatched_task
        processor.queue = [queued_task1, queued_task2]
        
        # Mock the respond_failure methods
        dispatched_task.respond_failure = Mock()
        queued_task1.respond_failure = Mock()
        queued_task2.respond_failure = Mock()
        
        result = coordinator._evict(processor, user_message="Test message")
        
        assert result is True
        assert processor._has_been_terminated is True
        assert processor.dispatched_task is None
        assert len(processor.queue) == 0
        
        # Check failure messages were sent
        dispatched_task.respond_failure.assert_called_once()
        queued_task1.respond_failure.assert_called_once()
        queued_task2.respond_failure.assert_called_once()
        
        # Check message format
        dispatched_call_args = dispatched_task.respond_failure.call_args[0][0]
        assert "Task interrupted during execution." in dispatched_call_args
        assert "Test message" in dispatched_call_args
    
    def test_get_eviction_message_contexts(self):
        """Test _get_eviction_message for different contexts."""
        coordinator = MockCoordinator()
        
        assert coordinator._get_eviction_message("execution") == "Task interrupted during execution."
        assert coordinator._get_eviction_message("queue") == "Task removed from queue."
        assert coordinator._get_eviction_message("unknown") == "Task interrupted."
    
    def test_handle_processor_failure(self):
        """Test _handle_processor_failure calls evict with correct message."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        processor.set_status(ProcessorStatus.TERMINATED)
        
        # Set up custom failure message
        coordinator.failure_messages[ProcessorStatus.TERMINATED] = "Custom termination message"
        
        with patch.object(coordinator, 'evict_processor') as mock_evict:
            coordinator._handle_processor_failure(processor)
            
            mock_evict.assert_called_once_with("test-processor", user_message="Custom termination message")


class TestCoordinatorProcessorLifecycle:
    """Test cases for processor lifecycle management."""
    
    def test_deactivate_processor_success(self):
        """Test successful processor deactivation."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        coordinator.active_processors["test-processor"] = processor
        
        coordinator._deactivate_processor("test-processor")
        
        assert "test-processor" not in coordinator.active_processors
        assert "test-processor" in coordinator.inactive_processors
        assert coordinator.inactive_processors["test-processor"] is processor
    
    def test_deactivate_processor_not_found(self):
        """Test deactivating processor that's not in active processors."""
        coordinator = MockCoordinator()
        
        # Should not raise exception
        coordinator._deactivate_processor("non-existent")
        
        assert len(coordinator.active_processors) == 0
        assert len(coordinator.inactive_processors) == 0
    
    def test_deactivate_processor_already_inactive(self):
        """Test deactivating processor that's already in inactive."""
        coordinator = MockCoordinator()
        processor = MockProcessor("test-processor")
        coordinator.active_processors["test-processor"] = processor
        coordinator.inactive_processors["test-processor"] = processor  # Shouldn't happen but test handles it
        
        coordinator._deactivate_processor("test-processor")
        
        assert "test-processor" not in coordinator.active_processors
        assert "test-processor" in coordinator.inactive_processors
    
    def test_cleanup_all_processors(self):
        """Test cleanup_all_processors clears all processors."""
        coordinator = MockCoordinator()
        coordinator.active_processors["active1"] = MockProcessor("active1")
        coordinator.inactive_processors["inactive1"] = MockProcessor("inactive1")
        
        coordinator._cleanup_all_processors()
        
        assert len(coordinator.active_processors) == 0
        assert len(coordinator.inactive_processors) == 0
    
    def test_create_processor_default(self):
        """Test default _create_processor creates base Processor."""
        coordinator = MockCoordinator()
        
        # Use the actual base class method
        with patch('src.processing.base.Processor') as mock_processor_class:
            mock_processor = Mock()
            mock_processor_class.return_value = mock_processor
            
            # Call the base class method directly
            from src.coordination.base import Coordinator
            result = Coordinator._create_processor(coordinator, "test-key")
            
            mock_processor_class.assert_called_once_with("test-key", max_retries=coordinator.max_retries)
            assert result is mock_processor


class TestCoordinatorDeployment:
    """Test cases for processor deployment functionality."""
    
    def test_deploy_with_no_backend_handle(self):
        """Test deployment when no backend handle is available."""
        coordinator = MockCoordinator()
        processor1 = MockProcessor("proc1")
        processor2 = MockProcessor("proc2")
        processors = [processor1, processor2]
        
        coordinator._deploy(processors)
        
        assert processor1.backend_status == DeploymentStatus.DEPLOYED
        assert processor2.backend_status == DeploymentStatus.DEPLOYED
    
    def test_deploy_with_backend_handle(self):
        """Test deployment warning when backend handle is available but not overridden."""
        coordinator = MockCoordinator()
        coordinator.set_backend_handle(Mock())
        processor = MockProcessor("proc1") 
        
        with patch('src.coordination.base.logger') as mock_logger:
            coordinator._deploy([processor])
            
            # Should log warning about backend handle being available
            mock_logger.warning.assert_called_once()
            assert processor.backend_status == DeploymentStatus.DEPLOYED
    
    def test_deploy_empty_list(self):
        """Test deployment with empty processor list."""
        coordinator = MockCoordinator()
        
        # Should not raise exception
        coordinator._deploy([])
        
        # No processors to deploy, should return early


class TestCoordinatorUtilities:
    """Test cases for utility methods and edge cases."""
    
    def test_interrupt_sleep(self):
        """Test _interrupt_sleep sets the event."""
        coordinator = MockCoordinator()
        
        # Initially should not be set
        assert not coordinator._sleep_event.is_set()
        
        coordinator._interrupt_sleep()
        
        # Should now be set
        assert coordinator._sleep_event.is_set()
    
    def test_get_state_with_processors(self):
        """Test get_state includes processor information."""
        coordinator = MockCoordinator()
        
        # Add mock processors
        active_proc = MockProcessor("active1")
        inactive_proc = MockProcessor("inactive1") 
        coordinator.active_processors["active1"] = active_proc
        coordinator.inactive_processors["inactive1"] = inactive_proc
        
        # Mock get_state method on processors
        active_proc.get_state = Mock(return_value={"id": "active1", "status": "active"})
        inactive_proc.get_state = Mock(return_value={"id": "inactive1", "status": "inactive"})
        
        state = coordinator.get_state()
        
        assert len(state["active_processors"]) == 1
        assert len(state["inactive_processors"]) == 1
        assert state["total_processors"] == 2
        assert state["active_processors"][0] == {"id": "active1", "status": "active"}
        assert state["inactive_processors"][0] == {"id": "inactive1", "status": "inactive"}
    
    def test_start_stop_coordinator(self):
        """Test basic start/stop functionality."""
        coordinator = MockCoordinator()
        
        assert coordinator.running is False
        
        with patch('threading.Thread') as mock_thread:
            mock_thread_instance = Mock()
            mock_thread.return_value = mock_thread_instance
            
            coordinator.start()
            
            assert coordinator.running is True
            mock_thread.assert_called_once()
            mock_thread_instance.start.assert_called_once()
        
        coordinator.stop()
        assert coordinator.running is False
    
    def test_start_already_running(self):
        """Test starting coordinator that's already running."""
        coordinator = MockCoordinator()
        coordinator.running = True
        
        with patch('src.coordination.base.logger') as mock_logger:
            coordinator.start()
            
            mock_logger.warning.assert_called_once_with("Coordinator is already running")
    
    def test_stop_not_running(self):
        """Test stopping coordinator that's not running."""
        coordinator = MockCoordinator()
        
        with patch('src.coordination.base.logger') as mock_logger:
            coordinator.stop()
            
            mock_logger.warning.assert_called_once_with("Coordinator is not running")


if __name__ == "__main__":
    pytest.main([__file__])