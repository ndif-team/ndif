"""Basic proof that the API unit-test runner discovers and executes tests."""


def test_pytest_runner() -> None:
    assert "hello world".upper() == "HELLO WORLD"
