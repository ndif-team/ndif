from common.providers.mailgun import MailgunProvider
import os
import pytest


@pytest.fixture(autouse=True)
def reset_mailgun_provider():
    MailgunProvider.api_key = None
    MailgunProvider.domain = None


def test_mailgun_provider(monkeypatch):
    monkeypatch.setenv("MAILGUN_DOMAIN", "nnsight.net")
    monkeypatch.setenv("MAILGUN_API_KEY", "sk-123")

    MailgunProvider.from_env()

    assert MailgunProvider.domain == "nnsight.net"
    assert MailgunProvider.api_key == "sk-123"
