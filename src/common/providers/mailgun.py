import logging
import os

import requests


from . import Provider

logger = logging.getLogger("ndif")


class MailgunProvider(Provider):
    domain: str
    api_key: str

    @classmethod
    def from_env(cls) -> None:
        super().from_env()
        cls.domain = os.environ.get("MAILGUN_DOMAIN")
        cls.api_key = os.environ.get("MAILGUN_API_KEY")

    @classmethod
    def to_env(cls) -> dict:
        return {
            **super().to_env(),
            "MAILGUN_DOMAIN": cls.domain,
            "MAILGUN_API_KEY": cls.api_key,
        }

    @classmethod
    def connected(cls) -> bool:
        return cls.domain is not None and cls.api_key is not None

    @classmethod
    def send_email(cls, to: str, subject: str, text: str):
        logger.info(f"Sending email to {to} with subject {subject} and text {text}")
        requests.post(
            f"https://api.mailgun.net/v3/{cls.domain}/messages",
            auth=("api", cls.api_key),
            data={
                "from": f"NDIF <mailgun@{cls.domain}>",
                "to": [to],
                "subject": subject,
                "text": text,
            },
        )


MailgunProvider.from_env()
