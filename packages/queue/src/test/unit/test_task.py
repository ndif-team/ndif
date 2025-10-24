"""
Unit tests for the base Task class.
"""
import pytest
from unittest.mock import Mock
from datetime import datetime
from typing import Any

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from tasks.base import Task
from tasks.status import TaskStatus


class MockTask(Task):
    """Concrete implementation of Task for testing purposes."""
    
    def __init__(self, task_id: str, data: Any, position: int = None, status: TaskStatus = TaskStatus.QUEUED):
        super().__init__(task_id, data, position)
        self._status = status
        self._run_result = True
        self._run_called = False
        
    @property
    def status(self) -> TaskStatus:
        """Return the mocked status."""
        if self._failed:
            return TaskStatus.FAILED
        return self._status
    
    def set_status(self, status: TaskStatus):
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


class TestTask:
    """Test cases for the base Task class."""
    
    def test_task_initialization(self):
        """Test that Task initializes with correct values."""
        task_id = "test-123"
        data = {"test": "data"}
        position = 5
        
        task = MockTask(task_id, data, position)
        
        assert task.id == task_id
        assert task.data == data
        assert task.position == position
        assert task.retries == 0
        assert isinstance(task.created_at, datetime)
        assert not task._failed
        assert task._failure_reason is None
    
    def test_task_initialization_without_position(self):
        """Test that Task can be initialized without position."""
        task = MockTask("test-456", {"data": "value"})
        
        assert task.position is None
        assert task.status == TaskStatus.QUEUED
    
    def test_get_state_normal(self):
        """Test get_state returns correct information for normal task."""
        task = MockTask("test-789", {"key": "value"}, position=2)
        task.retries = 3
        
        state = task.get_state()
        
        assert state["id"] == "test-789"
        assert state["position"] == 2
        assert state["status"] == TaskStatus.QUEUED
        assert state["retries"] == 3
        assert "created_at" in state
        assert "failed" not in state
    
    def test_get_state_with_failure(self):
        """Test get_state includes failure reason when task has failed."""
        task = MockTask("test-fail", {"data": "value"})
        failure_reason = "Something went wrong"
        
        task.respond_failure(failure_reason)
        state = task.get_state()
        
        assert state["failed"] == failure_reason
        assert task._failed is True
    
    def test_respond_with_description(self):
        """Test respond method with custom description."""
        task = MockTask("test-respond", {"data": "value"})
        
        result = task.respond("Custom message")
        
        assert result == "Custom message"
    
    def test_respond_with_position(self):
        """Test respond method using position for default message."""
        task = MockTask("test-pos", {"data": "value"}, position=3)
        
        result = task.respond()
        
        assert result == "test-pos - Moved to position 4"
    
    def test_respond_with_status(self):
        """Test respond method using status for default message."""
        task = MockTask("test-status", {"data": "value"})
        task.set_status(TaskStatus.DISPATCHED)
        
        result = task.respond()
        
        assert result == "test-status - Status updated to TaskStatus.DISPATCHED"
    
    def test_respond_failure_with_description(self):
        """Test respond_failure with custom description."""
        task = MockTask("test-fail-desc", {"data": "value"})
        failure_desc = "Network timeout error"
        
        result = task.respond_failure(failure_desc)
        
        assert result == failure_desc
        assert task._failed is True
        assert task._failure_reason == failure_desc
        assert task.status == TaskStatus.FAILED
    
    def test_respond_failure_without_description(self):
        """Test respond_failure with default description."""
        task = MockTask("test-fail-default", {"data": "value"})
        
        result = task.respond_failure()
        
        expected = "test-fail-default - Task failed!"
        assert result == expected
        assert task._failed is True
        assert task._failure_reason == expected
    
    def test_run_abstract_method(self):
        """Test that run method works and can be mocked."""
        task = MockTask("test-run", {"data": "value"})
        mock_backend = Mock()
        
        result = task.run(mock_backend)
        
        assert result is True
        assert task._run_called is True
    
    def test_run_failure(self):
        """Test run method returning False."""
        task = MockTask("test-run-fail", {"data": "value"})
        task.set_run_result(False)
        mock_backend = Mock()
        
        result = task.run(mock_backend)
        
        assert result is False
        assert task._run_called is True
    
    def test_string_representation(self):
        """Test string representation of task."""
        task = MockTask("test-str", {"data": "value"})
        
        str_repr = str(task)
        
        assert str_repr == "MockTask(test-str)"
    
    def test_retries_increment(self):
        """Test that retries can be incremented."""
        task = MockTask("test-retries", {"data": "value"})
        
        assert task.retries == 0
        
        task.retries += 1
        assert task.retries == 1
        
        state = task.get_state()
        assert state["retries"] == 1