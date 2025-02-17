import pytest
import shutil
import os
from tests.utils.deployment import ServiceProfile

def test_pytest_working():
    """Basic test to verify pytest is working"""
    assert True, "Pytest is working!"

def test_imports():
    """Test that key dependencies are importable"""
    import pytest
    import ray
    import fastapi
    import requests
    import torch
    assert all([pytest, ray, fastapi, requests, torch]), "All required packages imported successfully"

def test_docker_compose_installed():
    """Test that docker-compose is installed"""
    assert shutil.which("docker-compose"), "docker-compose is installed"

def test_docker_compose_socket_mounted():
    """Test that the docker socket is mounted"""
    assert os.path.exists("/var/run/docker.sock"), "docker socket is mounted"


@pytest.mark.unit
def test_deployment_profiles():
    """Test that ServiceProfile enum contains expected values"""
    assert ServiceProfile.FRONTEND.value == "frontend"
    assert ServiceProfile.BACKEND.value == "backend"
    assert ServiceProfile.TELEMETRY.value == "telemetry"
    assert ServiceProfile.TEST.value == "test"