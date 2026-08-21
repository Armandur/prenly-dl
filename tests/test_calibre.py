"""Calibre-integrationen.

Anropen går mot en content server som testerna inte har, så det som
verifieras är argumenten som byggs, parsningen av svaret och att ett fel
mot Calibre inte fäller en nedladdning som redan lyckats.
"""

from pathlib import Path

import pytest

from prenly_dl import calibre


KONFIG = calibre.CalibreConfig(
    enabled=True,
    url="http://tervo2:8081/",
    library="Calibre_Library",
    username="rasmus",
    password="hemligt",
)


def test_adressen_far_formen_calibredb_vill_ha():
    # Avslutande snedstreck i inmatningen får inte ge dubbla.
    assert KONFIG.with_library == "http://tervo2:8081/#Calibre_Library"


def test_avstangd_integration_ar_inte_konfigurerad():
    assert not calibre.CalibreConfig().configured
    assert not KONFIG.__class__(enabled=False, url="http://a", library="b").configured
    # Påslagen men utan adress duger inte heller.
    assert not calibre.CalibreConfig(enabled=True, library="b").configured
    assert KONFIG.configured


def test_add_bygger_ratt_argument():
    argv = calibre.build_add_argv(
        KONFIG, Path("/ut/Hemslojd 2011-03-15.pdf"),
        title="Tidningen Hemslöjd 2011-03-15",
        authors="Tidningen Hemslöjd",
        series="Tidningen Hemslöjd",
        series_index="20110315",
        cover=Path("/cache/omslag.jpg"),
    )
    assert argv[:4] == ["calibredb", "add", "--with-library",
                        "http://tervo2:8081/#Calibre_Library"]
    assert argv[-1] == "/ut/Hemslojd 2011-03-15.pdf"
    for flagga, varde in [
        ("--title", "Tidningen Hemslöjd 2011-03-15"),
        ("--authors", "Tidningen Hemslöjd"),
        ("--series", "Tidningen Hemslöjd"),
        ("--series-index", "20110315"),
        ("--cover", "/cache/omslag.jpg"),
        ("--username", "rasmus"),
    ]:
        assert argv[argv.index(flagga) + 1] == varde, flagga


def test_losenordet_star_aldrig_i_argumenten():
    """argv syns i ps för varje användare på maskinen."""
    for argv in (
        calibre.build_add_argv(KONFIG, Path("/a.pdf"), title="T", authors="A"),
        calibre.build_add_format_argv(KONFIG, 12, Path("/a.pdf")),
        calibre.build_pubdate_argv(KONFIG, 12, "2011-03-15"),
        calibre.build_list_argv(KONFIG),
    ):
        assert "hemligt" not in argv
        assert argv[argv.index("--password") + 1] == "<stdin>"


def test_utan_anvandarnamn_skickas_inga_uppgifter():
    oppen = calibre.CalibreConfig(enabled=True, url="http://a:8081", library="b")
    argv = calibre.build_add_argv(oppen, Path("/a.pdf"), title="T", authors="A")
    assert "--username" not in argv and "--password" not in argv


def test_serienummer_ur_utgivningsdatum():
    assert calibre.series_index("2011-03-15") == "20110315"
    assert calibre.series_index(None) == ""
    assert calibre.series_index("okänt") == ""


@pytest.mark.parametrize(
    "utdata,vantat",
    [
        ("Added book ids: 1234", 1234),
        ("Added book ids: 12, 13", 12),
        ("Added book id: 7\n", 7),
        ("Merged book ids: 1779", 1779),
        # calibredb följer miljöns språk. Verifierat mot en svensk
        # calibre 9.11 som svarade så här, vilket kostade ett bok-id.
        ("Tillagda bokidentifierare: 1775", 1775),
        ("Tillagda bokidentifierare: 12, 13", 12),
        ("Backend not available", None),
        ("", None),
    ],
)
def test_parsar_bok_id_ur_svaret(utdata, vantat):
    assert calibre.parse_added_id(utdata) == vantat


def test_okant_svar_ger_fel_i_stallet_for_tyst_id_forlust(monkeypatch):
    monkeypatch.setattr(calibre, "run", lambda config, argv: "något oväntat")
    with pytest.raises(calibre.CalibreError, match="Kunde inte läsa bok-id"):
        calibre.push(KONFIG, Path("/a.pdf"), title="T", authors="A")


def test_befintligt_id_ersatter_filen_i_stallet_for_att_lagga_en_till():
    korda = []

    def falsk_run(config, argv):
        korda.append(argv)
        return ""

    original = calibre.run
    calibre.run = falsk_run
    try:
        bok_id = calibre.push(
            KONFIG, Path("/a.pdf"), title="T", authors="A", calibre_id=99
        )
    finally:
        calibre.run = original

    assert bok_id == 99
    assert len(korda) == 1
    assert korda[0][1] == "add_format"
    assert "99" in korda[0]


def test_utgivningsdatum_satts_efter_add():
    korda = []

    def falsk_run(config, argv):
        korda.append(argv)
        return "Added book ids: 55"

    original = calibre.run
    calibre.run = falsk_run
    try:
        bok_id = calibre.push(
            KONFIG, Path("/a.pdf"), title="T", authors="A", pubdate="2011-03-15"
        )
    finally:
        calibre.run = original

    assert bok_id == 55
    assert [argv[1] for argv in korda] == ["add", "set_metadata"]
    assert "pubdate:2011-03-15" in korda[1]


def test_saknad_binar_blir_calibrefel_och_inte_krasch():
    saknad = calibre.CalibreConfig(
        enabled=True, url="http://a:8081", library="b"
    )
    with pytest.raises(calibre.CalibreError, match="finns inte i PATH"):
        calibre.run(saknad, ["detta-kommando-finns-inte-nagonstans", "list"])


# --------------------------------------------------------------------------
# Integrationen i nedladdningsjobbet
# --------------------------------------------------------------------------


def _koa_nedladdning(db, force=False):
    from prenly_dl.db import repository as repo

    with db() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        site.auth_status = "ok"
        publication, _ = repo.upsert_publication(session, site, 4019, "Tidningen")
        publication.enabled = True
        issue, _ = repo.upsert_issue(
            session, publication, {"uid": "1", "name": "2026-01-01",
                                   "publication_date": "2026-01-01"}
        )
        job = repo.enqueue_download(session, issue, force=force)
        return job.id, issue.id


def _lyckad_nedladdning(monkeypatch):
    from prenly_dl import downloader, services

    def _hamtar(session, creds, title_id, uid, output_dir, prefix="",
                force=False, progress=None):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "x.pdf"
        path.write_bytes(b"pdf")
        return downloader.DownloadResult(path, 3)

    monkeypatch.setattr(services, "download_issue", _hamtar)


def test_avstangd_integration_ror_inte_calibre(db, monkeypatch):
    """Skyddar nedladdaren: utan uppgifter ska inget anrop ens försökas."""
    from prenly_dl import services
    from prenly_dl.db import repository as repo

    job_id, issue_id = _koa_nedladdning(db)
    _lyckad_nedladdning(monkeypatch)

    def _far_inte_koras(*args, **kwargs):
        raise AssertionError("calibredb anropades trots avstängd integration")

    monkeypatch.setattr(calibre, "run", _far_inte_koras)

    with db() as session:
        job = repo.claim_next_job(session)
        services.execute_job(session, job)

    with db() as session:
        from prenly_dl.db.models import Job, JobStatus

        job = session.get(Job, job_id)
        assert job.status == JobStatus.DONE.value
        assert "Calibre" not in (job.message or "")
        assert repo.get_issue(session, issue_id).calibre_id is None


def _sla_pa_calibre(db):
    from prenly_dl.db import repository as repo

    with db() as session:
        repo.set_setting(session, calibre.ENABLED_KEY, "on")
        repo.set_setting(session, calibre.URL_KEY, "http://tervo2:8081")
        repo.set_setting(session, calibre.LIBRARY_KEY, "Calibre_Library")


def test_lyckad_nedladdning_sparar_bok_id(db, monkeypatch):
    from prenly_dl import services
    from prenly_dl.db import repository as repo

    job_id, issue_id = _koa_nedladdning(db)
    _lyckad_nedladdning(monkeypatch)
    _sla_pa_calibre(db)
    monkeypatch.setattr(calibre, "run", lambda config, argv: "Added book ids: 77")

    with db() as session:
        job = repo.claim_next_job(session)
        services.execute_job(session, job)

    with db() as session:
        from prenly_dl.db.models import Job, JobStatus

        job = session.get(Job, job_id)
        assert job.status == JobStatus.DONE.value
        assert "i Calibre som 77" in job.message
        assert repo.get_issue(session, issue_id).calibre_id == 77


def test_calibrefel_marker_jobbet_men_faller_inte_nedladdningen(db, monkeypatch):
    from prenly_dl import services
    from prenly_dl.db import repository as repo
    from prenly_dl.db.models import IssueStatus

    job_id, issue_id = _koa_nedladdning(db)
    _lyckad_nedladdning(monkeypatch)
    _sla_pa_calibre(db)

    def _spricker(config, argv):
        raise calibre.CalibreError("servern svarade inte")

    monkeypatch.setattr(calibre, "run", _spricker)

    with db() as session:
        job = repo.claim_next_job(session)
        services.execute_job(session, job)

    with db() as session:
        from prenly_dl.db.models import Job, JobStatus

        job = session.get(Job, job_id)
        issue = repo.get_issue(session, issue_id)
        # PDF:en ligger på disk, alltså är nedladdningen giltig.
        assert job.status == JobStatus.DONE.value
        assert issue.status == IssueStatus.DONE.value
        assert issue.file_path
        assert "servern svarade inte" in job.message
        assert issue.calibre_id is None


def test_omhamtning_skapar_ingen_dubblett(db, monkeypatch):
    from prenly_dl import services
    from prenly_dl.db import repository as repo

    job_id, issue_id = _koa_nedladdning(db)
    _lyckad_nedladdning(monkeypatch)
    _sla_pa_calibre(db)
    monkeypatch.setattr(calibre, "run", lambda config, argv: "Added book ids: 77")

    with db() as session:
        services.execute_job(session, repo.claim_next_job(session))

    korda = []

    def _fangar(config, argv):
        korda.append(argv)
        return ""

    monkeypatch.setattr(calibre, "run", _fangar)
    with db() as session:
        issue = repo.get_issue(session, issue_id)
        repo.enqueue_download(session, issue, force=True)
    with db() as session:
        services.execute_job(session, repo.claim_next_job(session))

    # Andra varvet ska ersätta filen på bok 77, inte lägga in en ny bok.
    assert [argv[1] for argv in korda] == ["add_format"]
    assert "77" in korda[0]
    with db() as session:
        assert repo.get_issue(session, issue_id).calibre_id == 77


def test_misslyckat_utgivningsdatum_kostar_inte_bok_id():
    """Datumet är ett andra anrop. Går id:t förlorat blir nästa force en dubblett."""
    def _add_ok_men_datum_spricker(config, argv):
        if argv[1] == "set_metadata":
            raise calibre.CalibreError("fältet finns inte")
        return "Added book ids: 77"

    original = calibre.run
    calibre.run = _add_ok_men_datum_spricker
    try:
        bok_id = calibre.push(
            KONFIG, Path("/a.pdf"), title="T", authors="A", pubdate="2011-03-15"
        )
    finally:
        calibre.run = original

    assert bok_id == 77


def test_add_ror_aldrig_en_bok_som_redan_finns():
    """Uppmätt mot en riktig server 2026-08-21:

    overwrite ersätter den befintliga filen, ignore skriver bara filnamnet
    utan bok-id. new_record ger alltid ett läsbart id och lämnar den
    befintliga boken i fred.
    """
    argv = calibre.build_add_argv(KONFIG, Path("/a.pdf"), title="T", authors="A")
    assert argv[argv.index("--automerge") + 1] == "new_record"


def test_omslaget_foljer_med_till_calibre(db, monkeypatch, tmp_path):
    """Omslaget går via omslagscachen och inte samma väg som övrig metadata."""
    from prenly_dl import services
    from prenly_dl.db import repository as repo

    omslag = tmp_path / "omslag.jpg"
    omslag.write_bytes(b"jpeg")

    with db() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        site.auth_status = "ok"
        publication, _ = repo.upsert_publication(session, site, 4019, "Tidningen")
        publication.enabled = True
        issue, _ = repo.upsert_issue(
            session, publication,
            {"uid": "1", "name": "2026-01-01", "publication_date": "2026-01-01",
             "media": {"checksum": "abc123"}},
        )
        repo.enqueue_download(session, issue)

    _lyckad_nedladdning(monkeypatch)
    _sla_pa_calibre(db)
    monkeypatch.setattr(
        services, "fetch_and_cache_cover",
        lambda http, creds, title_id, checksum, stor=False: omslag,
    )
    korda = []

    def _fangar(config, argv):
        korda.append(argv)
        return "Added book ids: 5"

    monkeypatch.setattr(calibre, "run", _fangar)

    with db() as session:
        services.execute_job(session, repo.claim_next_job(session))

    add = korda[0]
    assert add[add.index("--cover") + 1] == str(omslag)


def test_sprakmiljon_tvingar_engelska():
    """Utskriften är enda vägen till bok-id:t, och den följer språket."""
    assert calibre.SPRAKMILJO["CALIBRE_OVERRIDE_LANG"] == "en"
    utdata = calibre.run(
        calibre.CalibreConfig(enabled=True, url="http://a", library="b"),
        ["sh", "-c", "echo $LC_ALL $CALIBRE_OVERRIDE_LANG"],
    )
    assert utdata.strip() == "C en"


def test_stdin_stangs_aven_utan_losenord():
    """Med --username men utan lösenord frågar calibredb efter ett.

    Ärvd stdin hade hängt arbetaren tills timeouten löste ut, alltså fem
    minuter per utgåva.
    """
    utan_losen = calibre.CalibreConfig(
        enabled=True, url="http://a:8081", library="b", username="rasmus"
    )
    # Ett kommando som läser stdin till EOF returnerar bara om pipen stängs.
    utdata = calibre.run(
        utan_losen,
        ["python3", "-c", "import sys; print('las:' + repr(sys.stdin.read()))"],
    )
    assert utdata.strip() == "las:''"


def test_titelmonstret_ersatter_falten():
    assert calibre.format_title(
        "{publikation} {utgåva}", publikation="Hemslöjd", utgava="2023-2"
    ) == "Hemslöjd 2023-2"
    assert calibre.format_title(
        "{utgåva}", publikation="Hemslöjd", utgava="2023-2"
    ) == "2023-2"
    assert calibre.format_title(
        "Hemslöjd {datum}", publikation="H", utgava="x", datum="2023-03-27"
    ) == "Hemslöjd 2023-03-27"


def test_trasigt_titelmonster_faller_tillbaka_i_stallet_for_att_krascha():
    """Mönstret skrivs för hand, och ett fel får inte fälla nedladdningen."""
    for trasigt in ("{okänt}", "{trasig", "", "   "):
        assert calibre.format_title(
            trasigt, publikation="Hemslöjd", utgava="2023-2"
        ) == "Hemslöjd 2023-2"


def test_publikationens_monster_anvands_vid_nedladdning(db, monkeypatch):
    from prenly_dl import services
    from prenly_dl.db import repository as repo

    _koa_nedladdning(db)
    _lyckad_nedladdning(monkeypatch)
    _sla_pa_calibre(db)
    with db() as session:
        publication = repo.list_publications(session)[0]
        publication.calibre_title = "{utgåva} ({publikation})"
        session.add(publication)

    korda = []
    monkeypatch.setattr(
        calibre, "run", lambda config, argv: korda.append(argv) or "Added book ids: 9"
    )

    with db() as session:
        services.execute_job(session, repo.claim_next_job(session))

    add = korda[0]
    assert add[add.index("--title") + 1] == "2026-01-01 (Tidningen)"


# --------------------------------------------------------------------------
# Tema och nummer
# --------------------------------------------------------------------------


def test_temat_ar_forstasidans_rubrik():
    """Uppmätt mot Prenly: förstasidesartikelns rubrik är numrets tema."""
    from prenly_dl.api import front_page_headline

    data = {
        "sections": [
            {"slug": "annat", "article_ids": [2]},
            {"slug": "forstasidan", "article_ids": [1]},
        ],
        "articles": [
            {"id": 1, "headline": "Hitta gnistan!"},
            {"id": 2, "headline": "Notiser"},
        ],
    }
    assert front_page_headline(data) == "Hitta gnistan!"
    assert front_page_headline({"articles": [], "sections": []}) == ""
    # En sektion utan artikel får inte ge fel rubrik.
    assert front_page_headline(
        {"sections": [{"slug": "forstasidan", "article_ids": []}],
         "articles": [{"id": 9, "headline": "Fel"}]}
    ) == ""


def test_monstret_ger_samma_namn_som_biblioteket_redan_har():
    assert calibre.format_title(
        "{år}-{nummer} - {tema}",
        publikation="Tidningen Hemslöjd", utgava="2024-04-02",
        datum="2024-04-02", tema="Hitta gnistan!", nummer=2,
    ) == "2024-2 - Hitta gnistan!"


def test_monster_med_tomt_falt_faller_tillbaka():
    """Utan temat hade mönstret gett strängen '2024- -'."""
    assert calibre.format_title(
        "{år}-{nummer} - {tema}",
        publikation="Tidningen Hemslöjd", utgava="2024-04-02",
        datum="2024-04-02", tema="", nummer=None,
    ) == "Tidningen Hemslöjd 2024-04-02"


def test_serienumret_blir_ar_punkt_nummer():
    assert calibre.series_index("2024-04-02", 2) == "2024.2"
    # Utan nummer får datumet duga.
    assert calibre.series_index("2024-04-02") == "20240402"


def test_numret_ar_utgavans_ordning_inom_aret(db, temp_env):
    from prenly_dl.db import repository as repo

    with db() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        publication, _ = repo.upsert_publication(session, site, 4019, "Hemslöjd")
        # Uppmätta utgivningsdatum för Tidningen Hemslöjd 2024.
        datum = ["2024-02-05", "2024-04-02", "2024-05-27",
                 "2024-08-22", "2024-10-11", "2024-12-03"]
        for index, dag in enumerate(datum):
            repo.upsert_issue(
                session, publication,
                {"uid": str(index), "name": dag, "publication_date": dag},
            )
        publication.catalogue_complete = True
        session.flush()

        nummer = {
            i.publication_date: repo.issue_number_in_year(session, i)
            for i in repo.list_issues(session, publication.id)
        }

    assert nummer["2024-04-02"] == 2
    assert nummer["2024-10-11"] == 5
    assert nummer["2024-12-03"] == 6


def test_numret_sparras_nar_katalogen_inte_ar_komplett(db, temp_env):
    """Halva året i katalogen hade gett fel nummer utan att det syns."""
    from prenly_dl.db import repository as repo

    with db() as session:
        site = repo.upsert_site(
            session, "https://a.se", {"textalk-auth": "k", "auth": "t"}
        )
        publication, _ = repo.upsert_publication(session, site, 4019, "Hemslöjd")
        for index, dag in enumerate(["2024-10-11", "2024-12-03"]):
            repo.upsert_issue(
                session, publication,
                {"uid": str(index), "name": dag, "publication_date": dag},
            )
        publication.catalogue_complete = False
        session.flush()

        for issue in repo.list_issues(session, publication.id):
            assert repo.issue_number_in_year(session, issue) is None
