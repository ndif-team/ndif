#running pytest
cd ndif_root

pytest -q src/services/ray/tests/unit/deployments/controller/cluster/test_deployment.py

pytest -q src/services/ray/tests/unit/deployments/controller/cluster/test_deployment.py::test_cache_logs_and_returns_none_on_failure