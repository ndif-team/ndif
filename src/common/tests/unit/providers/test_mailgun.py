from common.providers.mailgun import MailgunProvider
import pytest


@pytest.fixture(autouse=True)
def reset_mailgun_provider():
    MailgunProvider.api_key = None
    MailgunProvider.domain = None


def test_mailgun_provider_from_env(monkeypatch):
    monkeypatch.setenv("MAILGUN_DOMAIN", "nnsight.net")
    monkeypatch.setenv("MAILGUN_API_KEY", "sk-123")

    MailgunProvider.from_env()

    assert MailgunProvider.domain == "nnsight.net"
    assert MailgunProvider.api_key == "sk-123"


def test_mailgun_provider_to_env(monkeypatch):
    monkeypatch.setenv("MAILGUN_DOMAIN", "nnsight.net")
    monkeypatch.setenv("MAILGUN_API_KEY", "sk-123")

    MailgunProvider.from_env()

    result = MailgunProvider.to_env()
    assert result["MAILGUN_DOMAIN"] == "nnsight.net"
    assert result["MAILGUN_API_KEY"] == "sk-123"


def test_mailgun_provider_connected_success(monkeypatch):
    monkeypatch.setenv("MAILGUN_DOMAIN", "nnsight.net")
    monkeypatch.setenv("MAILGUN_API_KEY", "sk-123")
    MailgunProvider.from_env()
    assert MailgunProvider.connected()


def test_mailgun_provider_connected_fail():
    assert not MailgunProvider.connected()
