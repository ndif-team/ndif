import subprocess
from typing import List, Optional, Union
from pathlib import Path
from enum import Enum

class ServiceProfile(Enum):
    """
    Docker compose profiles for different service groups
    """
    NDIF = "ndif"            # All core NDIF services
    FRONTEND = "frontend"     # API, Redis, Minio
    BACKEND = "backend"       # Ray head/workers
    TELEMETRY = "telemetry"  # Prometheus, Grafana, InfluxDB, Loki
    TEST = "test"            # Test service

class ServiceDeployer:
    def __init__(self, environment: str = "dev"):
        """
        Initialize service deployer for a specific environment.
        
        Args:
            environment: The deployment environment ('dev' or 'prod')
        """
        if environment not in ["dev", "prod"]:
            raise ValueError(f"Invalid environment: {environment}. Must be 'dev' or 'prod'")
            
        self.env = environment
        self.compose_file = Path(f"src/tests/utils/compose/{self.env}/docker-compose.yml")
        
        if not self.compose_file.exists():
            raise FileNotFoundError(f"Docker compose file not found at {self.compose_file}")

    def _run_compose_command(self, command: List[str]) -> subprocess.CompletedProcess:
        """
        Run a docker-compose command with the configured compose file.
        
        Args:
            command: List of command arguments to pass to docker-compose
            
        Returns:
            CompletedProcess instance from subprocess.run
        """
        base_cmd = ["docker-compose", "-f", str(self.compose_file)]
        full_cmd = base_cmd + command
        
        try:
            result = subprocess.run(
                full_cmd,
                check=True,
                capture_output=True,
                text=True
            )
            return result
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Docker compose command failed: {e.stderr}")

    def deploy(self, profiles: Union[ServiceProfile, List[ServiceProfile]], detach: bool = True):
        """
        Deploy services based on profiles using docker-compose.
        
        Args:
            profiles: ServiceProfile enum or list of ServiceProfiles to deploy
            detach: Whether to run containers in detached mode
        """
        if isinstance(profiles, ServiceProfile):
            profiles = [profiles]
            
        try:
            cmd = []
            for profile in profiles:
                cmd.extend(["--profile", profile.value])
                
            cmd.append("up")
            if detach:
                cmd.append("-d")
                
            self._run_compose_command(cmd)
            
        except Exception as e:
            raise RuntimeError(f"Error deploying services: {str(e)}")

    def cleanup(self, profiles: Optional[List[ServiceProfile]] = None):
        """
        Cleanup services for specified profiles or all if none specified using docker-compose.
        
        Args:
            profiles: Optional list of ServiceProfiles to clean up. If None, cleans up all.
        """
        try:
            cmd = []
            if profiles:
                for profile in profiles:
                    cmd.extend(["--profile", profile.value])
                    
            cmd.append("down")
            cmd.extend(["--remove-orphans"])
            
            self._run_compose_command(cmd)
                
        except Exception as e:
            raise RuntimeError(f"Error cleaning up services: {str(e)}")

    @staticmethod
    def get_required_profiles(test_type: str) -> List[ServiceProfile]:
        """
        Get required profiles based on test type
        """
        if test_type == "unit":
            return []
        elif test_type == "integration":
            return [ServiceProfile.FRONTEND]
        elif test_type == "e2e":
            return [ServiceProfile.FRONTEND, ServiceProfile.BACKEND, ServiceProfile.TELEMETRY]
        else:
            raise ValueError(f"Unknown test type: {test_type}")