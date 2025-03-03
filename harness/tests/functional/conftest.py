import pytest
import nnsight

from .util import CONFIG

def pytest_generate_tests(metafunc):
    """
    This function is called for each test function.
    It will parametrize the case with all the appropriate parameters.
    """

    if metafunc.cls:
        test_name = metafunc.cls.name()
        if test_name == "WarmUp":
            # For warmup tests, only parametrize with the model key
            if "model" in metafunc.fixturenames:
                # Parametrize with all models from config
                metafunc.parametrize("model", list(CONFIG["models"]), indirect=True)
        else:
            # Get the test config
            if test_name in CONFIG["tests"].keys():
                test = CONFIG["tests"][test_name]
                # Parametrize the test with the config params
                metafunc.parametrize(test[0], test[1], indirect=["model"])


@pytest.fixture(scope="module")
def model(request):
    return nnsight.modeling.mixins.RemoteableMixin.from_model_key(request.param)