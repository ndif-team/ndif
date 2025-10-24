"""
Unit tests for the base Processor class.
"""
import pytest
from unittest.mock import Mock, patch
from datetime import datetime
from typing import Any

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from src.processing.base import Processor
from src.processing.status import ProcessorStatus, DeploymentStatus
from src.tasks.status import TaskStatus

# Define MockTask directly here to avoid import issues
class MockTask:
    """Concrete implementation of Task for testing purposes."""
    
    def __init__(self, task_id: str, data: Any, position: int = None, status=None):
        self.id = task_id
        self.data = data
        self.position = position
        self.retries = 0
        self.created_at = datetime.now()
        self._failed = False
        self._failure_reason = None
        self._status = status or TaskStatus.QUEUED
        self._run_result = True
        self._run_called = False
        
    @property
    def status(self):
        """Return the mocked status."""
        if self._failed:
            return TaskStatus.FAILED
        return self._status
    
    def set_status(self, status):
        """Helper to change status during tests."""
        self._status = status
        
    def run(self, backend_handle) -> bool:
        """Mock implementation that tracks if run was called."""
        _ = backend_handle  # Unused parameter for abstract method implementation
        self._run_called = True
        return self._run_result
    
    def set_run_result(self, result: bool):
        """Helper to set run result for testing."""
        self._run_result = result
        
    def respond(self, description: str = None) -> str:
        """Mock respond method."""
        if description:
            return description
        elif self.position is not None:
            return f"{self.id} - Moved to position {self.position + 1}"
        else:
            return f"{self.id} - Status updated to {self.status}"
    
    def respond_failure(self, description: str = None) -> str:
        """Mock respond_failure method."""
        if description is None:
            description = f"{self.id} - Task failed!"
        
        self._failed = True
        self._failure_reason = description
        return description
    
    def get_state(self):
        """Mock get_state method."""
        state = {
            "id": self.id,
            "position": self.position,
            "status": self.status,
            "retries": self.retries,
            "created_at": self.created_at.isoformat(),
        }
        
        if self._failed and self._failure_reason:
            state["failed"] = self._failure_reason
        
        return state


class MockProcessor(Processor[MockTask]):
    """Concrete implementation of Processor for testing purposes."""
    
    def __init__(self, processor_id: str, **kwargs):
        super().__init__(processor_id, **kwargs)
        self._handle = None
        self._restart_called = False
        
    @property
    def handle(self):
        """Override to return a mockable handle."""
        return self._handle
        
    def set_handle(self, handle):
        """Helper to set handle for testing."""
        self._handle = handle
        
    def _restart_implementation(self):
        """Track when restart implementation is called."""
        self._restart_called = True


class TestProcessorInitialization:
    """Test cases for Processor initialization and basic properties."""
    
    def test_processor_initialization_defaults(self):
        """Test Processor initializes with correct default values."""
        processor = MockProcessor("test-processor")
        
        assert processor.id == "test-processor"
        assert processor.max_retries == 3
        assert processor.max_tasks is None
        assert processor.last_dispatched is None
        assert processor.dispatched_task is None
        assert processor.deletion_queue == []
        assert processor._has_been_terminated is False
        assert processor.backend_status is None
        assert len(processor.queue) == 0
    
    def test_processor_initialization_custom_values(self):
        """Test Processor initialization with custom parameters."""
        processor = MockProcessor(
            "custom-processor",
            max_retries=5,
            max_tasks=10
        )
        
        assert processor.id == "custom-processor"
        assert processor.max_retries == 5
        assert processor.max_tasks == 10
    
    def test_queue_property_returns_list(self):
        """Test that queue property returns the internal queue list."""
        processor = MockProcessor("test-queue")
        
        # Should be empty initially
        assert isinstance(processor.queue, list)
        assert len(processor.queue) == 0
        
        # Should be the same object as _queue
        assert processor.queue is processor._queue
    
    def test_handle_property_defaults_to_none(self):
        """Test that handle property defaults to None."""
        processor = MockProcessor("test-handle")
        
        assert processor.handle is None
    
    def test_handle_property_can_be_set(self):
        """Test that handle property can be set via set_handle helper."""
        processor = MockProcessor("test-handle-set")
        mock_handle = Mock()
        
        processor.set_handle(mock_handle)
        
        assert processor.handle is mock_handle
    
    def test_get_state_empty_processor(self):
        """Test get_state returns correct info for empty processor."""
        processor = MockProcessor("test-state")
        
        state = processor.get_state()
        
        assert state["id"] == "test-state"
        assert state["status"] == ProcessorStatus.INACTIVE  # No tasks, no handle
        assert state["dispatched_task"] is None
        assert state["queue"] == []
        assert state["last_dispatched"] is None
        assert state["deletion_queue"] == []
        assert state["has_been_terminated"] is False
        assert state["backend_status"] is None
    
    def test_string_representation(self):
        """Test string representation of processor."""
        processor = MockProcessor("test-str-repr")
        
        str_repr = str(processor)
        
        assert str_repr == "MockProcessor(test-str-repr)"


class TestProcessorEnqueueDequeue:
    """Test cases for enqueue/dequeue functionality."""
    
    def test_enqueue_success(self):
        """Test successful enqueue operation."""
        processor = MockProcessor("test-enqueue")
        task = MockTask("task-1", {"data": "test"})
        
        result = processor.enqueue(task)
        
        assert result is True
        assert len(processor.queue) == 1
        assert processor.queue[0] is task
        assert task.position == 0  # Position gets updated by enqueue
    
    def test_enqueue_multiple_tasks(self):
        """Test enqueuing multiple tasks updates positions correctly."""
        processor = MockProcessor("test-enqueue-multi")
        task1 = MockTask("task-1", {"data": "test1"})
        task2 = MockTask("task-2", {"data": "test2"})
        
        result1 = processor.enqueue(task1)
        result2 = processor.enqueue(task2)
        
        assert result1 is True
        assert result2 is True
        assert len(processor.queue) == 2
        assert processor.queue[0] is task1
        assert processor.queue[1] is task2
    
    def test_enqueue_at_max_capacity(self):
        """Test enqueue fails when at max capacity."""
        processor = MockProcessor("test-enqueue-max", max_tasks=2)
        task1 = MockTask("task-1", {"data": "test1"})
        task2 = MockTask("task-2", {"data": "test2"})
        task3 = MockTask("task-3", {"data": "test3"})
        
        # Fill to capacity
        assert processor.enqueue(task1) is True
        assert processor.enqueue(task2) is True
        
        # This should fail
        result = processor.enqueue(task3)
        
        assert result is False
        assert len(processor.queue) == 2
        assert task3._failed is True
        assert "max capacity" in task3._failure_reason
    
    def test_dequeue_success(self):
        """Test successful dequeue operation."""
        processor = MockProcessor("test-dequeue")
        task = MockTask("task-1", {"data": "test"})
        processor.enqueue(task)
        
        dequeued_task = processor.dequeue()
        
        assert dequeued_task is task
        assert len(processor.queue) == 0
        assert dequeued_task.position is None  # Position cleared on dequeue
    
    def test_dequeue_empty_queue(self):
        """Test dequeue returns None when queue is empty."""
        processor = MockProcessor("test-dequeue-empty")
        
        result = processor.dequeue()
        
        assert result is None
    
    def test_dequeue_updates_positions(self):
        """Test dequeue updates positions of remaining tasks."""
        processor = MockProcessor("test-dequeue-positions")
        task1 = MockTask("task-1", {"data": "test1"})
        task2 = MockTask("task-2", {"data": "test2"})
        task3 = MockTask("task-3", {"data": "test3"})
        
        processor.enqueue(task1)
        processor.enqueue(task2)
        processor.enqueue(task3)
        
        # Dequeue first task
        dequeued = processor.dequeue()
        
        assert dequeued is task1
        assert len(processor.queue) == 2
        assert processor.queue[0] is task2
        assert processor.queue[1] is task3
        # Note: update_positions will be called, updating task positions


class TestProcessorStatus:
    """Test cases for processor status property."""
    
    def test_status_inactive_empty_processor(self):
        """Test status is INACTIVE when no tasks and no dispatched task."""
        processor = MockProcessor("test-status-inactive")
        
        assert processor.status == ProcessorStatus.INACTIVE
    
    def test_status_terminated(self):
        """Test status is TERMINATED when termination flag is set."""
        processor = MockProcessor("test-status-terminated")
        processor._has_been_terminated = True
        
        assert processor.status == ProcessorStatus.TERMINATED
    
    def test_status_draining_at_capacity(self):
        """Test status is DRAINING when at max capacity."""
        processor = MockProcessor("test-status-draining", max_tasks=2)
        task1 = MockTask("task-1", {"data": "test1"})
        task2 = MockTask("task-2", {"data": "test2"})
        
        processor.enqueue(task1)
        processor.enqueue(task2)
        
        assert processor.status == ProcessorStatus.DRAINING
    
    def test_status_active_with_handle(self):
        """Test status is ACTIVE when handle is available."""
        processor = MockProcessor("test-status-active")
        task = MockTask("task-1", {"data": "test"})
        processor.enqueue(task)
        processor.set_handle(Mock())
        
        assert processor.status == ProcessorStatus.ACTIVE
    
    def test_status_unavailable_backend(self):
        """Test status is UNAVAILABLE when backend status is CANT_ACCOMMODATE."""
        processor = MockProcessor("test-status-unavailable")
        task = MockTask("task-1", {"data": "test"})
        processor.enqueue(task)
        processor.backend_status = DeploymentStatus.CANT_ACCOMMODATE
        
        assert processor.status == ProcessorStatus.UNAVAILABLE
    
    def test_status_provisioning(self):
        """Test status is PROVISIONING when scheduled but no handle yet."""
        processor = MockProcessor("test-status-provisioning")
        task = MockTask("task-1", {"data": "test"})
        processor.enqueue(task)
        processor.backend_status = DeploymentStatus.FREE
        # No handle set, so should be provisioning
        
        assert processor.status == ProcessorStatus.PROVISIONING
    
    def test_status_uninitialized_default(self):
        """Test status is UNINITIALIZED when no handle and no backend status."""
        processor = MockProcessor("test-status-uninitialized")
        task = MockTask("task-1", {"data": "test"})
        processor.enqueue(task)
        # No handle, no backend_status
        
        assert processor.status == ProcessorStatus.UNINITIALIZED


class TestProcessorUtilityMethods:
    """Test cases for processor utility methods."""
    
    def test_has_task_in_queue(self):
        """Test has_task finds task in queue."""
        processor = MockProcessor("test-has-request")
        task = MockTask("target-task", {"data": "test"})
        processor.enqueue(task)
        
        assert processor.has_task("target-task") is True
        assert processor.has_task("non-existent") is False
    
    def test_has_task_dispatched(self):
        """Test has_task finds dispatched task."""
        processor = MockProcessor("test-has-dispatched")
        task = MockTask("dispatched-task", {"data": "test"})
        processor.dispatched_task = task
        
        assert processor.has_task("dispatched-task") is True
        assert processor.has_task("non-existent") is False
    
    def test_notify_pending_task(self):
        """Test notify_pending_task calls respond on all queued tasks."""
        processor = MockProcessor("test-notify")
        task1 = MockTask("task-1", {"data": "test1"})
        task2 = MockTask("task-2", {"data": "test2"})
        
        processor.enqueue(task1)
        processor.enqueue(task2)
        
        # Mock respond AFTER enqueue to avoid interfering with enqueue's call
        task1.respond = Mock(return_value="response1")
        task2.respond = Mock(return_value="response2")
        
        processor.notify_pending_task("Custom message")
        
        task1.respond.assert_called_once_with("Custom message")
        task2.respond.assert_called_once_with("Custom message")
    
    def test_notify_pending_task_default_message(self):
        """Test notify_pending_task uses default message."""
        processor = MockProcessor("test-notify-default")
        task = MockTask("task-1", {"data": "test"})
        
        processor.enqueue(task)
        
        # Mock respond AFTER enqueue
        task.respond = Mock(return_value="response")
        processor.notify_pending_task()
        
        task.respond.assert_called_once_with("A task is pending... stand by.")


class TestProcessorRestart:
    """Test cases for processor restart functionality."""
    
    def test_restart_clears_state(self):
        """Test restart clears all processor state."""
        processor = MockProcessor("test-restart")
        task = MockTask("task-1", {"data": "test"})
        
        # Set up some state
        processor.enqueue(task)
        processor.dispatched_task = MockTask("dispatched", {"data": "dispatched"})
        processor.deletion_queue.append("delete-me")
        processor._has_been_terminated = True
        
        processor.restart()
        
        assert processor.dispatched_task is None
        assert len(processor.deletion_queue) == 0
        assert len(processor.queue) == 0  # Should be cleared
        assert processor._has_been_terminated is False
        assert processor._restart_called is True
    
    def test_restart_calls_implementation(self):
        """Test restart calls _restart_implementation."""
        processor = MockProcessor("test-restart-impl")
        
        processor.restart()
        
        assert processor._restart_called is True


class TestProcessorLifecycle:
    """Test cases for processor lifecycle advancement."""
    
    def test_advance_lifecycle_no_tasks(self):
        """Test advance_lifecycle returns False when no work to do."""
        processor = MockProcessor("test-lifecycle-empty")
        
        result = processor.advance_lifecycle()
        
        assert result is False
    
    def test_advance_lifecycle_dequeues_task(self):
        """Test advance_lifecycle dequeues task when none dispatched."""
        processor = MockProcessor("test-lifecycle-dequeue")
        task = MockTask("task-1", {"data": "test"})
        processor.enqueue(task)
        processor.set_handle(Mock())  # Need handle to not be invariant
        
        result = processor.advance_lifecycle()
        
        assert result is True  # Should have dequeued and dispatched
        assert processor.dispatched_task is task
        assert len(processor.queue) == 0
    
    def test_advance_lifecycle_dispatches_pending_task(self):
        """Test advance_lifecycle dispatches pending task."""
        processor = MockProcessor("test-lifecycle-dispatch")
        task = MockTask("task-1", {"data": "test"}, status=TaskStatus.PENDING)
        processor.dispatched_task = task
        processor.set_handle(Mock())
        
        with patch.object(processor, '_dispatch', return_value=True) as mock_dispatch:
            result = processor.advance_lifecycle()
            
            assert result is True
            mock_dispatch.assert_called_once()
    
    def test_advance_lifecycle_handles_completed_task(self):
        """Test advance_lifecycle handles completed task."""
        processor = MockProcessor("test-lifecycle-completed")
        task = MockTask("task-1", {"data": "test"}, status=TaskStatus.COMPLETED)
        processor.dispatched_task = task
        # Set a handle so the processor status is not invariant
        processor.set_handle(Mock())
        
        with patch.object(processor, '_handle_completed_dispatch') as mock_handle:
            result = processor.advance_lifecycle()
            
            assert result is True
            mock_handle.assert_called_once()
    
    def test_advance_lifecycle_handles_failed_task(self):
        """Test advance_lifecycle handles failed task."""
        processor = MockProcessor("test-lifecycle-failed")
        task = MockTask("task-1", {"data": "test"}, status=TaskStatus.FAILED)
        processor.dispatched_task = task
        # Set a handle so the processor status is not invariant
        processor.set_handle(Mock())
        
        with patch.object(processor, '_handle_failed_dispatch') as mock_handle:
            result = processor.advance_lifecycle()
            
            assert result is True
            mock_handle.assert_called_once()
    
    def test_advance_lifecycle_invariant_status(self):
        """Test advance_lifecycle returns False for invariant statuses."""
        processor = MockProcessor("test-lifecycle-invariant")
        task = MockTask("task-1", {"data": "test"})
        processor.enqueue(task)
        # No handle, so status will be UNINITIALIZED (invariant)
        
        result = processor.advance_lifecycle()
        
        assert result is False


class TestProcessorDispatch:
    """Test cases for task dispatch functionality."""
    
    def test_dispatch_success(self):
        """Test successful task dispatch."""
        processor = MockProcessor("test-dispatch-success")
        task = MockTask("task-1", {"data": "test"})
        mock_handle = Mock()
        processor.dispatched_task = task
        processor.set_handle(mock_handle)
        
        task.respond = Mock()
        
        result = processor._dispatch()
        
        assert result is True
        assert task._run_called is True
        assert isinstance(processor.last_dispatched, datetime)
        task.respond.assert_called_once_with(description="Dispatching task...")
    
    def test_dispatch_task_run_failure(self):
        """Test dispatch when task.run() returns False."""
        processor = MockProcessor("test-dispatch-fail")
        task = MockTask("task-1", {"data": "test"})
        task.set_run_result(False)
        processor.dispatched_task = task
        processor.set_handle(Mock())
        
        task.respond = Mock()
        
        result = processor._dispatch()
        
        assert result is False
        assert task._run_called is True
        assert processor.last_dispatched is None
    
    def test_dispatch_task_run_exception(self):
        """Test dispatch when task.run() raises exception."""
        processor = MockProcessor("test-dispatch-exception")
        task = MockTask("task-1", {"data": "test"})
        processor.dispatched_task = task
        processor.set_handle(Mock())
        
        task.respond = Mock()
        # Mock run to raise an exception
        task.run = Mock(side_effect=Exception("Run failed"))
        
        result = processor._dispatch()
        
        assert result is False
        assert processor.last_dispatched is None
    
    def test_dispatch_respond_failure(self):
        """Test dispatch continues when task.respond() fails."""
        processor = MockProcessor("test-dispatch-respond-fail")
        task = MockTask("task-1", {"data": "test"})
        processor.dispatched_task = task
        processor.set_handle(Mock())
        
        task.respond = Mock(side_effect=Exception("Respond failed"))
        
        # Should still succeed despite respond failing
        result = processor._dispatch()
        
        assert result is True
        assert task._run_called is True


class TestProcessorFailureHandling:
    """Test cases for failure handling and retry logic."""
    
    def test_handle_failed_dispatch_retry(self):
        """Test failure handling with retries available."""
        processor = MockProcessor("test-fail-retry", max_retries=3)
        task = MockTask("task-1", {"data": "test"})
        task.retries = 1  # Has retries remaining
        processor.dispatched_task = task
        
        with patch.object(processor, '_dispatch', return_value=True) as mock_dispatch:
            processor._handle_failed_dispatch()
            
            # Should retry
            mock_dispatch.assert_called_once()
            assert task.retries == 2
            assert processor.dispatched_task is task  # Still dispatched
    
    def test_handle_failed_dispatch_max_retries(self):
        """Test failure handling when max retries exceeded."""
        processor = MockProcessor("test-fail-max-retries", max_retries=2)
        task = MockTask("task-1", {"data": "test"})
        task.retries = 2  # At max retries
        processor.dispatched_task = task
        
        task.respond_failure = Mock(return_value="Failure response")
        
        processor._handle_failed_dispatch()
        
        # Should not retry, should call respond_failure
        task.respond_failure.assert_called_once_with(description="Task failed after maximum retries.")
        assert processor.dispatched_task is None
    
    def test_handle_failed_dispatch_respond_failure_exception(self):
        """Test failure handling when respond_failure raises exception."""
        processor = MockProcessor("test-fail-respond-exception", max_retries=1)
        task = MockTask("task-1", {"data": "test"})
        task.retries = 1  # At max retries
        processor.dispatched_task = task
        
        task.respond_failure = Mock(side_effect=Exception("Respond failed"))
        
        # Should not raise exception, should clear dispatched_task
        processor._handle_failed_dispatch()
        
        assert processor.dispatched_task is None
    
    def test_get_failure_message_default(self):
        """Test default failure message."""
        processor = MockProcessor("test-failure-message")
        
        message = processor._get_failure_message()
        
        assert message == "Task failed after maximum retries."
    
    def test_handle_completed_dispatch(self):
        """Test handling completed dispatch."""
        processor = MockProcessor("test-completed")
        task = MockTask("task-1", {"data": "test"})
        processor.dispatched_task = task
        
        processor._handle_completed_dispatch()
        
        assert processor.dispatched_task is None


class TestProcessorDeletionQueue:
    """Test cases for deletion queue processing."""
    
    def test_process_deletion_queue_empty(self):
        """Test process_deletion_queue does nothing when queue is empty."""
        processor = MockProcessor("test-deletion-empty")
        task = MockTask("task-1", {"data": "test"})
        processor.enqueue(task)
        
        processor.process_deletion_queue()
        
        # Nothing should change
        assert len(processor.queue) == 1
    
    def test_process_deletion_queue_remove_from_queue(self):
        """Test deletion queue removes tasks from queue."""
        processor = MockProcessor("test-deletion-queue")
        task1 = MockTask("task-1", {"data": "test1"})
        task2 = MockTask("task-2", {"data": "test2"})
        task3 = MockTask("task-3", {"data": "test3"})
        
        processor.enqueue(task1)
        processor.enqueue(task2)
        processor.enqueue(task3)
        
        # Mark task-2 for deletion
        processor.deletion_queue.append("task-2")
        
        with patch.object(processor, 'update_positions') as mock_update:
            processor.process_deletion_queue()
            
            assert len(processor.queue) == 2
            assert processor.queue[0] is task1
            assert processor.queue[1] is task3
            assert len(processor.deletion_queue) == 0
            mock_update.assert_called_once()
    
    def test_process_deletion_queue_cancel_dispatched(self):
        """Test deletion queue cancels dispatched task."""
        processor = MockProcessor("test-deletion-dispatched")
        task = MockTask("dispatched-task", {"data": "test"})
        processor.dispatched_task = task
        processor.deletion_queue.append("dispatched-task")
        
        with patch.object(processor, '_cancel_dispatched_task') as mock_cancel:
            processor.process_deletion_queue()
            
            mock_cancel.assert_called_once()
            assert len(processor.deletion_queue) == 0
    
    def test_cancel_dispatched_task_cleanup(self):
        """Test _cancel_dispatched_task performs cleanup."""
        processor = MockProcessor("test-cancel-cleanup")
        task1 = MockTask("task-1", {"data": "test1"})
        task2 = MockTask("task-2", {"data": "test2"})
        processor.enqueue(task1)
        processor.enqueue(task2)
        processor.deletion_queue.append("some-id")
        processor.dispatched_task = MockTask("dispatched", {"data": "dispatched"})
        
        with patch.object(processor, 'update_positions') as mock_update:
            processor._cancel_dispatched_task()
            
            assert len(processor.deletion_queue) == 0
            assert processor.dispatched_task is None
            mock_update.assert_called_once()


class TestProcessorUpdatePositions:
    """Test cases for position update functionality."""
    
    def test_update_positions_all_tasks(self):
        """Test update_positions updates all task positions."""
        processor = MockProcessor("test-positions-all")
        task1 = MockTask("task-1", {"data": "test1"})
        task2 = MockTask("task-2", {"data": "test2"})
        task3 = MockTask("task-3", {"data": "test3"})
        
        processor.enqueue(task1)
        processor.enqueue(task2)
        processor.enqueue(task3)
        
        # Mock respond AFTER enqueue and manually mess up positions to test update
        task1.respond = Mock()
        task2.respond = Mock()
        task3.respond = Mock()
        
        task1.position = 5
        task2.position = 10
        task3.position = None
        
        processor.update_positions()
        
        # Check positions are correct
        assert task1.position == 0
        assert task2.position == 1
        assert task3.position == 2
        
        # All should have been responded to (position changed)
        task1.respond.assert_called_once()
        task2.respond.assert_called_once()
        task3.respond.assert_called_once()
    
    def test_update_positions_specific_indices(self):
        """Test update_positions with specific indices."""
        processor = MockProcessor("test-positions-specific")
        task1 = MockTask("task-1", {"data": "test1"})
        task2 = MockTask("task-2", {"data": "test2"})
        task3 = MockTask("task-3", {"data": "test3"})
        
        # Mock respond to track calls
        task1.respond = Mock()
        task2.respond = Mock()
        task3.respond = Mock()
        
        processor.enqueue(task1)
        processor.enqueue(task2)
        processor.enqueue(task3)
        
        # Only update specific indices
        processor.update_positions([0, 2])
        
        # Only task1 and task3 should be updated (indices 0 and 2)
        assert task1.position == 0
        assert task3.position == 2
        # task2 position should remain unchanged from enqueue
        task1.respond.assert_called_once()
        task3.respond.assert_called_once()
        # task2 respond may or may not be called depending on if position changed


if __name__ == "__main__":
    pytest.main([__file__])