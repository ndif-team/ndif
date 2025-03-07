from typing import TYPE_CHECKING
from .util import BaseTest
import pytest

if TYPE_CHECKING:
     from nnsight.modeling.mixins import RemoteableMixin
     from .util import BackendTest

@pytest.mark.dependency(name="warmup", scope="session")
@pytest.mark.order(1)
class TestWarmUp(BaseTest):
    """Test class for warming up the model.
    
    This test ensures the model is properly initialized by running multiple
    simple inference requests. This helps stabilize performance metrics for
    subsequent tests.
    """

    @pytest.fixture(scope="class")
    def num_requests(self):
        """Number of warm up requests to perform per model.
        
        Returns:
            int: Number of warmup requests (default: 3)
        """
        return 3

    def test(self, model: "RemoteableMixin", num_requests: int):
        """Parametrized test function.
        
        Args:
            model (RemoteableMixin): model.
            num_requests (int): number of requests to make for this test.
        """

        def warmup(backend: "BackendTest") -> None:
            """ Warmup the model.
            
            Args:
                model (RemoteableMixin): model.
            """

            with model.trace(["Hello"], backend=backend):
                out = model.output.save()

        assert self.run_test(warmup, model, num_requests=num_requests)