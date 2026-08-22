import logging

import requests
from fastapi.testclient import TestClient

from prenly_dl import downloader, notify, services
from prenly_dl.db import repository as repo
from prenly_dl.db.models import AuthStatus
from prenly_dl.errors import PrenlyAuthError, PrenlyError


class _Svar:
    def raise_for_status(self):
        return None


class _Session:
    def __init__(self):
        self.anrop = []

    def post(self, url, **kwargs):
        self.anrop.append((url, kwargs))
        return _Svar()


def _koa_nedladdning(db, title_id=1):
    with db() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        repo.set_site_status(session, site, AuthStatus.OK)
        publication, _ = repo.upsert_publication(
            session, site, title_id, "Ålandsposten"
        )
        issue, _ = repo.upsert_issue(
            session,
            publication,
            {"uid": str(title_id), "name": "Vårutgåvan"},
        )
        job = repo.enqueue_download(session, issue)
        return job.id


def test_ntfy_skickar_svensk_text_i_json_kroppen():
    http = _Session()
    kanal = notify.NtfyChannel(
        "https://ntfy.example.se/", "svc_prenly_dl", "token", session=http
    )

    kanal.send("Utgåva hämtad", "Åland, vårutgåva: 12 sidor med å ä ö")

    assert http.anrop == [
        (
            "https://ntfy.example.se/",
            {
                "json": {
                    "topic": "svc_prenly_dl",
                    "title": "Utgåva hämtad",
                    "message": "Åland, vårutgåva: 12 sidor med å ä ö",
                },
                "headers": {"Authorization": "Bearer token"},
                "timeout": notify.DEFAULT_TIMEOUT,
            },
        )
    ]


def test_kanalfel_loggas_utan_att_kastas(caplog):
    class _TrasigKanal(notify.NotificationChannel):
        name = "trasig"

        def send(self, titel, meddelande):
            raise requests.ConnectionError("ingen kontakt")

    with caplog.at_level(logging.ERROR, logger="prenly_dl.notify"):
        notify.send_all([_TrasigKanal()], "Titel", "Meddelande")

    assert "Notifiering via trasig misslyckades oväntat" in caplog.text


def test_avstangda_notifieringar_skickar_ingenting():
    assert notify.build_channels(
        notify.NotifyConfig(
            enabled=False,
            url="https://ntfy.example.se",
            topic="svc_prenly_dl",
            webhook_url="https://example.se/webhook",
        )
    ) == []


def test_halvkonfigurerad_kanal_hoppas_over():
    http = _Session()
    kanaler = notify.build_channels(
        notify.NotifyConfig(
            enabled=True,
            url="https://ntfy.example.se",
            webhook_url="https://example.se/webhook",
        ),
        session=http,
    )

    notify.send_all(kanaler, "Titel", "Meddelande")

    assert len(kanaler) == 1
    assert http.anrop[0][0] == "https://example.se/webhook"


def test_lyckad_nedladdning_notifierar_men_overhoppad_inte(
    db, monkeypatch, tmp_path
):
    skickade = []
    monkeypatch.setattr(
        services,
        "_skicka_notis",
        lambda session, titel, meddelande: skickade.append((titel, meddelande)),
    )
    monkeypatch.setattr(services, "build_session", lambda: None)
    monkeypatch.setattr(
        services,
        "download_issue",
        lambda *args, **kwargs: downloader.DownloadResult(tmp_path / "utgåva.pdf", 14),
    )

    _koa_nedladdning(db)
    with db() as session:
        services.run_download_job(session, repo.claim_next_job(session))

    assert skickade == [
        ("Utgåva hämtad", "Ålandsposten, Vårutgåvan: 14 sidor")
    ]

    monkeypatch.setattr(
        services,
        "download_issue",
        lambda *args, **kwargs: downloader.DownloadResult(
            tmp_path / "utgåva.pdf", 0, skipped=True
        ),
    )
    _koa_nedladdning(db, title_id=2)
    with db() as session:
        services.run_download_job(session, repo.claim_next_job(session))

    assert len(skickade) == 1


def test_misslyckad_nedladdning_notifierar_med_felet(db, monkeypatch):
    skickade = []
    monkeypatch.setattr(
        services,
        "_skicka_notis",
        lambda session, titel, meddelande: skickade.append((titel, meddelande)),
    )
    monkeypatch.setattr(services, "build_session", lambda: None)

    def _spricker(*args, **kwargs):
        raise PrenlyError("nätverket dog på sidan 4")

    monkeypatch.setattr(services, "download_issue", _spricker)
    _koa_nedladdning(db)

    with db() as session:
        services.execute_job(session, repo.claim_next_job(session))

    assert skickade == [
        (
            "Nedladdning misslyckades",
            "Ålandsposten, Vårutgåvan: nätverket dog på sidan 4",
        )
    ]


def test_auth_error_notifierar_bara_vid_forsta_overgangen(db, monkeypatch):
    skickade = []
    monkeypatch.setattr(
        services,
        "_skicka_notis",
        lambda session, titel, meddelande: skickade.append((titel, meddelande)),
    )

    with db() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        repo.set_site_status(session, site, AuthStatus.OK)
        services._set_auth_error(
            session, site, PrenlyAuthError("Inte autentiserad.")
        )
        services._set_auth_error(
            session, site, PrenlyAuthError("Inte autentiserad.")
        )

    assert skickade == [
        (
            "Inloggningsuppgifter har slutat gälla",
            "https://a.se: Inte autentiserad.",
        )
    ]


def test_notify_installningar_sparas_och_tomt_tokenfalt_bevarar_token(
    temp_env, monkeypatch
):
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    with TestClient(app_module.create_app()) as klient:
        klient.post(
            "/settings/notify",
            data={
                "enabled": "on",
                "url": "https://ntfy.example.se/",
                "topic": "svc_prenly_dl",
                "token": "hemlig",
                "webhook_url": "https://example.se/webhook",
            },
        )
        klient.post(
            "/settings/notify",
            data={
                "enabled": "on",
                "url": "https://ny.example.se",
                "topic": "svc_prenly_dl",
                "token": "",
                "webhook_url": "",
            },
        )

    from prenly_dl.db.session import get_session

    with get_session() as session:
        config = notify.load_config(session)
    assert config == notify.NotifyConfig(
        enabled=True,
        url="https://ny.example.se",
        topic="svc_prenly_dl",
        token="hemlig",
        webhook_url="",
    )
