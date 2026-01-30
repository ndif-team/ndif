"""Pytest configuration for NDIF tests."""

import pytest
import os


def pytest_addoption(parser):
    """Add custom command line options."""
    parser.addoption(
        "--ndif-host",
        action="store",
        default="http://localhost:5001",
        help="NDIF API host URL",
    )
    parser.addoption(
        "--run-remote",
        action="store_true",
        default=False,
        help="Run tests that require a remote NDIF server",
    )


@pytest.fixture(scope="session")
def ndif_host(request):
    """Get the NDIF host from command line or environment."""
    return request.config.getoption("--ndif-host") or os.environ.get(
        "NDIF_HOST", "http://localhost:5001"
    )


@pytest.fixture(scope="session", autouse=True)
def configure_nnsight(ndif_host):
    """Configure nnsight for testing."""
    from nnsight import CONFIG

    CONFIG.API.HOST = ndif_host
    # Disable verbose logging during tests
    CONFIG.APP.REMOTE_LOGGING = False


def pytest_collection_modifyitems(config, items):
    """Skip remote tests unless --run-remote is specified."""
    if config.getoption("--run-remote"):
        return

    skip_remote = pytest.mark.skip(reason="Need --run-remote option to run")

    # Test classes that require remote NDIF server
    remote_test_classes = {
        # Security tests
        "TestAllowedOperations",
        "TestBlockedOperations",
        # NNsight remote feature tests
        "TestBasicTracing",
        "TestGeneration",
        "TestActivationModification",
        "TestGradients",
        "TestSessions",
        "TestCaching",
        "TestInvokers",
        "TestIteration",
        "TestAdhocModules",
        "TestEdgeCases",
        "TestPrintAndDebug",
        # Replica integration tests
        "TestReplicaRequests",
        "TestReplicaCLI",
    }

    for item in items:
        # Skip tests in classes that need remote execution
        if item.cls and item.cls.__name__ in remote_test_classes:
            item.add_marker(skip_remote)
