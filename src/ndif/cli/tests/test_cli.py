"""Integration tests for NDIF CLI.

These tests verify that CLI commands work correctly against a running NDIF cluster.

Run with:
    pytest src/ndif/cli/tests/test_cli.py
"""

import json
import pytest

from ndif.cli.cli import cli


# Expected services for a head node
EXPECTED_SERVICES = ["api", "ray", "broker", "object-store"]


class TestPrerequisites:
    """Prerequisite tests - verify NDIF is running before other tests."""

    def test_info_returns_valid_json(self, runner):
        """ndif info --json-output should return valid JSON."""
        result = runner.invoke(cli, ["info", "--json-output"])
        assert result.exit_code == 0, f"info command failed: {result.output}"
        data = json.loads(result.output)
        assert isinstance(data, dict), "info should return a JSON object"

    def test_session_exists(self, runner):
        """An active session should exist."""
        result = runner.invoke(cli, ["info", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data.get("session") is not None, "No active session - run 'ndif start' first"
        assert data["session"].get("id"), "Session should have an ID"

    def test_all_services_running(self, runner):
        """All expected services should be running."""
        result = runner.invoke(cli, ["info", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)

        services = data.get("services", {})
        assert services, "No services found in info output"

        not_running = []
        for service_name in EXPECTED_SERVICES:
            if service_name not in services:
                not_running.append(f"{service_name} (not configured)")
            elif not services[service_name].get("actually_running"):
                not_running.append(f"{service_name} (not running)")

        assert not not_running, f"Services not running: {', '.join(not_running)}"

    def test_each_service_running(self, runner):
        """Verify each service individually for clearer error messages."""
        result = runner.invoke(cli, ["info", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        services = data.get("services", {})

        for service_name in EXPECTED_SERVICES:
            assert service_name in services, f"Service '{service_name}' not configured"
            svc = services[service_name]
            assert svc.get("actually_running"), (
                f"Service '{service_name}' not running "
                f"(port {svc.get('configured_port')}, marked_running={svc.get('marked_running')})"
            )


class TestCLIBasics:
    """Basic CLI integration tests."""

    def test_status_shows_cluster_info(self, runner):
        """Status command should show cluster and deployment info."""
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "NDIF Cluster Status" in result.output
        assert "Cluster Resources:" in result.output
        assert "Active Deployments:" in result.output

    def test_status_json_returns_valid_json(self, runner):
        """Status --json-output should return parseable JSON."""
        result = runner.invoke(cli, ["status", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "deployments" in data or "cluster" in data

    def test_status_verbose_shows_details(self, runner):
        """Status --verbose should show detailed state."""
        result = runner.invoke(cli, ["status", "--verbose"])
        assert result.exit_code == 0
        assert "Detailed" in result.output or "Configuration" in result.output

    def test_queue_shows_processors(self, runner):
        """Queue command should show processor info."""
        result = runner.invoke(cli, ["queue"])
        assert result.exit_code == 0
        # Should show queue state (even if empty)
        assert "Queue" in result.output or "processor" in result.output.lower() or "No active" in result.output

    def test_env_shows_cluster_environment(self, runner):
        """Env command should show cluster Python environment."""
        result = runner.invoke(cli, ["env"])
        assert result.exit_code == 0
        assert "Python" in result.output

    def test_env_json_returns_valid_json(self, runner):
        """Env --json-output should return parseable JSON."""
        result = runner.invoke(cli, ["env", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "python_version" in data
        assert "packages" in data


class TestDeployEvictCycle:
    """Test deploy and evict commands work correctly."""

    def test_deploy_gpt2(self, runner):
        """Should be able to deploy gpt2 model."""
        result = runner.invoke(cli, ["deploy", "gpt2"])
        assert result.exit_code == 0
        # Should show deployment result
        assert "gpt2" in result.output.lower()
        assert "Deploying" in result.output

    def test_deploy_shows_in_status(self, runner):
        """After deploy, model should appear in status."""
        # First deploy
        runner.invoke(cli, ["deploy", "gpt2"])

        # Then check status
        result = runner.invoke(cli, ["status", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)

        # Model should be in deployments (HOT or in process)
        deployments = data.get("deployments", {})
        found = any("gpt2" in str(v).lower() for v in deployments.values())
        assert found, "gpt2 should appear in deployments after deploy"

    def test_evict_gpt2(self, runner):
        """Should be able to evict gpt2 model."""
        # First ensure it's deployed
        runner.invoke(cli, ["deploy", "gpt2"])

        # Then evict
        result = runner.invoke(cli, ["evict", "gpt2"])
        assert result.exit_code == 0
        assert "gpt2" in result.output.lower() or "evict" in result.output.lower()


class TestDeployDedicated:
    """Test dedicated deployment flag."""

    def test_deploy_dedicated_flag(self, runner):
        """Deploy with --dedicated should mark model as dedicated."""
        result = runner.invoke(cli, ["deploy", "gpt2", "--dedicated"])
        assert result.exit_code == 0
        assert "dedicated" in result.output.lower() or "Deploying" in result.output

    def test_dedicated_shows_in_status(self, runner):
        """Dedicated deployment should show dedicated=true in status."""
        # Deploy as dedicated
        runner.invoke(cli, ["deploy", "gpt2", "--dedicated"])

        # Check status
        result = runner.invoke(cli, ["status", "--json-output"])
        assert result.exit_code == 0
        data = json.loads(result.output)

        # Find gpt2 deployment and check dedicated flag
        deployments = data.get("deployments", {})
        for key, dep in deployments.items():
            if "gpt2" in str(dep).lower():
                assert dep.get("dedicated") is True, "gpt2 should be marked as dedicated"
                break
