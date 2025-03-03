from typing import TYPE_CHECKING
import pytest
from .util import BaseTest, skip_test

if TYPE_CHECKING:
     from nnsight.modeling.mixins import RemoteableMixin
     from .util import BackendTest

@skip_test
@pytest.mark.dependency(depends=["warmup"], scope="session")
class TestTokenGeneration(BaseTest):
    """ Token Generation validation test case, with the following parameters:
        - model: model.
        - num_requests (int): number of requests to make for this test.
        - batch_size (int): batch size.
        - prompt_length (int): prompt length.
        - num_tokens (int): number of tokens to generate.
    """
    
    @pytest.fixture(scope="class")
    def batch_size(self):
        """Batch size for model inference.
        
        Returns:
            int: defaults to 1.
        """
        return 1
    
    @pytest.fixture(scope="class")
    def prompt_length(self):
        """Length of the input prompt.
        
        Returns:
            int: defaults to 1.
        """
        return 1
    
    @pytest.fixture(scope="class")
    def num_tokens(self):
        """Number of tokens to generate.
        
        Returns:
            int: defaults to 10.
        """
        return 10
    
    def test(self, model: "RemoteableMixin", num_requests: int, batch_size: int, prompt_length: int, num_tokens: int):
        print(num_requests, batch_size, prompt_length, num_tokens)
        """Parametrized test function.
        
        Args:
            model (RemoteableMixin): model.
            num_requests (int): number of requests to make for this test.
            batch_size (int): batch size.
            prompt_length (int): prompt length.
            num_tokens (int): number of tokens to generate.
        """

        def token_generation(backend: "BackendTest") -> None:
            """ Token Generation.
            
            Args:
                backend: Backend to use for generation
            """

            inputs = ["Hello" + ("Hello" * (prompt_length - 1))] * batch_size

            with model.generate(inputs, max_new_tokens=num_tokens, backend=backend, remote=True):
                
                out = model.generator.output.save()


        assert self.run_test(token_generation, model, num_requests, num_tokens=num_tokens, batch_size=batch_size)

