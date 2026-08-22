"""Skicka notifieringar via ntfy och webhook (TASK-1462).

Notifieringar är best effort: ett fel här får aldrig ändra utfallet för
jobbet som utlöste meddelandet. send_all loggar därför varje fel och
fortsätter med nästa kanal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

# Nycklarna i settings-tabellen.
ENABLED_KEY = "notify_enabled"
URL_KEY = "notify_url"
TOPIC_KEY = "notify_topic"
TOKEN_KEY = "notify_token"
WEBHOOK_URL_KEY = "notify_webhook_url"

DEFAULT_TIMEOUT = 10


class NotifyError(RuntimeError):
    """En notifieringskanal kunde inte leverera meddelandet."""


@dataclass(frozen=True)
class NotifyConfig:
    enabled: bool = False
    url: str = ""
    topic: str = ""
    token: str = ""
    webhook_url: str = ""


def load_config(session) -> NotifyConfig:
    from .db import repository as repo

    return NotifyConfig(
        enabled=repo.get_setting(session, ENABLED_KEY, "") == "on",
        url=repo.get_setting(session, URL_KEY, ""),
        topic=repo.get_setting(session, TOPIC_KEY, ""),
        token=repo.get_setting(session, TOKEN_KEY, ""),
        webhook_url=repo.get_setting(session, WEBHOOK_URL_KEY, ""),
    )


class NotificationChannel:
    """Gemensamt gränssnitt för notifieringskanaler."""

    name = "kanal"

    def send(self, titel: str, meddelande: str) -> None:
        """Leverera titel och meddelande eller kasta NotifyError."""
        raise NotImplementedError


class NtfyChannel(NotificationChannel):
    """Publicera till ett ntfy-topic via JSON-endpointen.

    JSON-kroppen mot {url}/ används i stället för Title-headern mot
    {url}/{topic}. HTTP-headers måste vara latin-1-säkra, medan
    publikations- och utgåvenamn är svensk text och kan innehålla å, ä och ö.
    """

    name = "ntfy"

    def __init__(
        self,
        url: str,
        topic: str,
        token: str = "",
        *,
        session: requests.Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url.rstrip("/")
        self.topic = topic
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout

    def send(self, titel: str, meddelande: str) -> None:
        if not self.url or not self.topic:
            raise NotifyError("ntfy saknar adress eller topic")
        # Bara den ASCII-baserade tokenen ligger i headers. Svensk text
        # stannar i JSON-kroppen där requests kodar den säkert.
        huvuden = {}
        if self.token:
            huvuden["Authorization"] = f"Bearer {self.token}"
        innehall = {"topic": self.topic, "title": titel, "message": meddelande}
        try:
            svar = self.session.post(
                f"{self.url}/",
                json=innehall,
                headers=huvuden,
                timeout=self.timeout,
            )
            svar.raise_for_status()
        except requests.RequestException as error:
            raise NotifyError(
                f"ntfy kunde inte publicera till {self.topic!r}: {error}"
            ) from error


class WebhookChannel(NotificationChannel):
    """Skicka titel och meddelande som JSON till en webhook."""

    name = "webhook"

    def __init__(
        self,
        url: str,
        *,
        session: requests.Session | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.url = url
        self.session = session or requests.Session()
        self.timeout = timeout

    def send(self, titel: str, meddelande: str) -> None:
        if not self.url:
            raise NotifyError("webhook saknar adress")
        try:
            svar = self.session.post(
                self.url,
                json={"title": titel, "message": meddelande},
                timeout=self.timeout,
            )
            svar.raise_for_status()
        except requests.RequestException as error:
            raise NotifyError(f"webhook-anropet misslyckades: {error}") from error


def build_channels(
    config: NotifyConfig,
    *,
    session: requests.Session | None = None,
) -> list[NotificationChannel]:
    """Bygg bara de kanaler som är påslagna och fullständigt inställda."""
    if not config.enabled:
        return []
    kanaler: list[NotificationChannel] = []
    if config.url.strip() and config.topic.strip():
        kanaler.append(
            NtfyChannel(
                config.url.strip(),
                config.topic.strip(),
                config.token,
                session=session,
            )
        )
    if config.webhook_url.strip():
        kanaler.append(WebhookChannel(config.webhook_url.strip(), session=session))
    return kanaler


def send_all(
    kanaler: list[NotificationChannel], titel: str, meddelande: str
) -> None:
    """Skicka genom alla kanaler utan att notifieringsfel når anroparen."""
    for kanal in kanaler:
        try:
            kanal.send(titel, meddelande)
        except NotifyError as error:
            logger.error("Notifiering via %s misslyckades: %s", kanal.name, error)
        except Exception as error:  # noqa: BLE001 - en kanalbugg får inte fälla jobbet
            logger.error(
                "Notifiering via %s misslyckades oväntat: %s", kanal.name, error
            )
