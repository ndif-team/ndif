import os
import pytest
from typing import Literal

TestType = Literal["unit", "integration", "e2e"]

def pytest_addoption(parser):
    parser.addoption(
        "--test-type",
        action="store",
        default="unit",
        help="Type of tests to run: unit, integration, or e2e"
    )
    parser.addoption(
        "--deploy-services",
        action="store_true",
        help="Deploy required services before running tests"
    )

@pytest.fixture(scope="session")
def test_type(request) -> TestType:
    return request.config.getoption("--test-type")

@pytest.fixture(scope="session")
def should_deploy_services(request) -> bool:
    return request.config.getoption("--deploy-services")

@pytest.fixture(scope="session", autouse=True)
def setup_test_environment(test_type, should_deploy_services):
    """Setup the test environment based on test type"""
    if should_deploy_services:
        if test_type == "e2e":
            # Deploy full stack
            deploy_full_stack()
        elif test_type == "integration":
            # Deploy minimal required services
            deploy_minimal_services()
    
    yield
    
    # Cleanup after tests
    if should_deploy_services:
        cleanup_services() 