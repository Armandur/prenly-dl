"""Matchning mot böcker som redan finns i Calibre.

Vyn föreslår, användaren avgör. Uppmätt mot ett riktigt bibliotek på 1167
böcker: sökning enbart på utgivningsdatum gav 434 kandidater, nästan alla
romaner som råkade ges ut samma dag som ett tidningsnummer.
"""

import pytest

from prenly_dl import calibre, calibre_api, calibre_match
from prenly_dl.db import repository as repo


KONFIG = calibre.CalibreConfig(
    enabled=True, url="http://tervo2:8881", library="ebooks",
    username="prenly-dl", password="hemligt",
)


def _bok(titel, forfattare, datum, storlek=1000):
    return {
        "title": titel,
        "authors": [forfattare],
        # Calibre serialiserar saknat datum som strängen "None".
        "pubdate": f"{datum}T00:00:00+00:00" if datum else "None",
        "format_metadata": {"pdf": {"size": storlek}},
    }


@pytest.fixture
def bibliotek(monkeypatch):
    """Ett litet bibliotek med en riktig träff och en datumkrock."""
    bocker = {
        1541: _bok("2024-2 - Hitta gnistan!", "Hemslöjd", "2024-04-02", 15433544),
        1686: _bok("Just for the Summer", "Abby Jimenez", "2024-04-02"),
        1900: _bok("Helt orelaterad", "Någon Annan", "2019-01-01"),
    }
    monkeypatch.setattr(
        calibre_api, "fetch_library",
        lambda config, force=False: calibre_api.Bibliotek(bocker, 0.0),
    )
    calibre_match.glom_forslag()
    return bocker


def _publikation(session, namn="Tidningen Hemslöjd", monster=""):
    site = repo.upsert_site(session, "https://a.se", {"textalk-auth": "k", "auth": "t"})
    site.auth_status = "ok"
    publication, _ = repo.upsert_publication(session, site, 4019, namn)
    publication.calibre_title = monster
    session.add(publication)
    session.flush()
    return publication


def test_datumkrock_ensam_racker_inte(db, temp_env, bibliotek):
    """Utan tröskeln hade tidningen kunnat läggas som format på en roman."""
    with db() as session:
        publication = _publikation(session, namn="Bilaga")
        repo.upsert_issue(
            session, publication,
            {"uid": "1", "name": "2024-04-02", "publication_date": "2024-04-02"},
        )
        forslag = calibre_match.find_candidates(session, KONFIG)

    # Varken titel eller författare talar för någon av de två böckerna.
    assert forslag == []


def test_forfattaren_racker_som_stod(db, temp_env, bibliotek):
    with db() as session:
        publication = _publikation(session)
        repo.upsert_issue(
            session, publication,
            {"uid": "1", "name": "2024-04-02", "publication_date": "2024-04-02"},
        )
        forslag = calibre_match.find_candidates(session, KONFIG)

    assert len(forslag) == 1
    kandidater = forslag[0].kandidater
    # Romanen delar datum men faller på tröskeln.
    assert [k.book_id for k in kandidater] == [1541]
    assert kandidater[0].poang == (
        calibre_match.POANG_FORFATTARE + calibre_match.POANG_DATUM
    )


def test_identisk_titel_rankas_hogst(db, temp_env, bibliotek):
    with db() as session:
        publication = _publikation(session, monster="{år}-{nummer} - {tema}")
        issue, _ = repo.upsert_issue(
            session, publication,
            {"uid": "1", "name": "2024-04-02", "publication_date": "2024-04-02"},
        )
        issue.theme = "Hitta gnistan!"
        publication.catalogue_complete = True
        # Numret kräver att hela året finns.
        for index, dag in enumerate(["2024-02-05", "2024-05-27"], start=2):
            repo.upsert_issue(
                session, publication,
                {"uid": str(index), "name": dag, "publication_date": dag},
            )
        session.flush()
        forslag = calibre_match.find_candidates(session, KONFIG)

    traff = [f for f in forslag if f.issue.publication_date == "2024-04-02"][0]
    assert traff.vantad_titel == "2024-2 - Hitta gnistan!"
    assert traff.kandidater[0].poang >= calibre_match.POANG_TITEL


def test_kopplade_och_ignorerade_kommer_inte_tillbaka(db, temp_env, bibliotek):
    with db() as session:
        publication = _publikation(session)
        issue, _ = repo.upsert_issue(
            session, publication,
            {"uid": "1", "name": "2024-04-02", "publication_date": "2024-04-02"},
        )
        assert len(calibre_match.find_candidates(session, KONFIG)) == 1

        issue.calibre_id = 1541
        session.flush()
        assert calibre_match.find_candidates(session, KONFIG) == []

        issue.calibre_id = None
        issue.calibre_ignored = True
        session.flush()
        assert calibre_match.find_candidates(session, KONFIG) == []


def test_avgransningen_slapper_bara_igenom_kandidater(db, temp_env, bibliotek):
    """Proxyrutterna går med de sparade uppgifterna."""
    with db() as session:
        publication = _publikation(session)
        repo.upsert_issue(
            session, publication,
            {"uid": "1", "name": "2024-04-02", "publication_date": "2024-04-02"},
        )
        tillatna = calibre_match.tillatna_bocker(session, KONFIG)

    assert 1541 in tillatna
    # Romanen som delar datum är ingen kandidat och ska inte gå att hämta.
    assert 1686 not in tillatna
    assert 1900 not in tillatna


# --------------------------------------------------------------------------
# Rutterna
# --------------------------------------------------------------------------


def _klient(temp_env, monkeypatch):
    from fastapi.testclient import TestClient
    import prenly_dl.web.app as app_module

    monkeypatch.setattr(app_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_scheduler", lambda: None)
    return TestClient(app_module.create_app())


def _uppsatt(db, monkeypatch, med_fil=None):
    with db() as session:
        publication = _publikation(session)
        publication.enabled = True
        issue, _ = repo.upsert_issue(
            session, publication,
            {"uid": "1", "name": "2024-04-02", "publication_date": "2024-04-02"},
        )
        if med_fil:
            issue.file_path = str(med_fil)
        repo.set_setting(session, calibre.ENABLED_KEY, "on")
        repo.set_setting(session, calibre.URL_KEY, "http://tervo2:8881")
        repo.set_setting(session, calibre.LIBRARY_KEY, "ebooks")
        session.commit()
        return issue.id


def test_koppling_skriver_ingenting_till_calibre(db, temp_env, bibliotek, monkeypatch):
    """Boken kan vara en post användaren själv lagt in.

    Ett klick som betyder "det här är samma utgåva" får inte röra den.
    """
    issue_id = _uppsatt(db, monkeypatch)

    def _far_inte_koras(*args, **kwargs):
        raise AssertionError("calibredb anropades av en koppling")

    monkeypatch.setattr(calibre, "run", _far_inte_koras)

    with _klient(temp_env, monkeypatch) as client:
        svar = client.post(
            f"/issues/{issue_id}/calibre/link", data={"book_id": 1541},
            headers={"HX-Request": "true"},
        )

    assert svar.status_code == 200
    with db() as session:
        issue = repo.get_issue(session, issue_id)
        assert issue.calibre_id == 1541
        # Utgåvan finns nu någonstans, även utan fil på vår disk.
        assert issue.status == "done"


def test_koppling_vagrar_en_bok_som_inte_ar_kandidat(db, temp_env, bibliotek, monkeypatch):
    issue_id = _uppsatt(db, monkeypatch)

    with _klient(temp_env, monkeypatch) as client:
        svar = client.post(
            f"/issues/{issue_id}/calibre/link", data={"book_id": 1686},
            headers={"HX-Request": "true"},
        )

    assert "finns inte bland förslagen" in svar.text
    with db() as session:
        assert repo.get_issue(session, issue_id).calibre_id is None


def test_ersatt_skickar_filen_men_koppling_gor_det_inte(
    db, temp_env, bibliotek, monkeypatch
):
    pdf = temp_env / "output" / "Tidningen Hemslöjd" / "utgava.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"pdf")
    issue_id = _uppsatt(db, monkeypatch, med_fil=pdf)
    korda = []
    monkeypatch.setattr(calibre, "run", lambda config, argv: korda.append(argv) or "")

    with _klient(temp_env, monkeypatch) as client:
        svar = client.post(
            f"/issues/{issue_id}/calibre/push", data={"book_id": 1541},
            headers={"HX-Request": "true"},
        )

    assert svar.status_code == 200
    assert [argv[1] for argv in korda] == ["add_format"]
    assert "1541" in korda[0]
    with db() as session:
        assert repo.get_issue(session, issue_id).calibre_id == 1541


def test_proxyn_ger_404_for_bok_utanfor_forslagen(db, temp_env, bibliotek, monkeypatch):
    _uppsatt(db, monkeypatch)
    monkeypatch.setattr(calibre_api, "fetch_cover", lambda config, book_id: b"jpeg")

    with _klient(temp_env, monkeypatch) as client:
        assert client.get("/calibre/cover/1686").status_code == 404
        assert client.get("/calibre/file/1900").status_code == 404
        assert client.get("/calibre/cover/1541").status_code == 200


def test_ersatt_vagrar_en_fil_utanfor_utdatakatalogen(
    db, temp_env, bibliotek, monkeypatch, tmp_path
):
    """Sökvägen går vidare till calibredb och kontrolleras som all annan."""
    utanfor = tmp_path / "nagon-annanstans.pdf"
    utanfor.write_bytes(b"pdf")
    issue_id = _uppsatt(db, monkeypatch, med_fil=utanfor)

    def _far_inte_koras(*args, **kwargs):
        raise AssertionError("calibredb fick en sökväg utanför utdatakatalogen")

    monkeypatch.setattr(calibre, "run", _far_inte_koras)

    with _klient(temp_env, monkeypatch) as client:
        svar = client.post(
            f"/issues/{issue_id}/calibre/push", data={"book_id": 1541},
            headers={"HX-Request": "true"},
        )

    assert "finns inte i utdatakatalogen" in svar.text
    with db() as session:
        assert repo.get_issue(session, issue_id).calibre_id is None


# --------------------------------------------------------------------------
# År och nummer ur bokens egen titel
# --------------------------------------------------------------------------


def test_saknat_datum_ser_ut_som_strangen_none():
    """Uppmätt: 179 av 1167 böcker hade pubdate satt till 'None'."""
    assert calibre_api.bok_datum({"pubdate": "None"}) == ""
    assert calibre_api.bok_datum({"pubdate": None}) == ""
    assert calibre_api.bok_datum({"pubdate": "0101-01-01T00:00:00+00:00"}) == ""
    assert calibre_api.bok_datum({"pubdate": "2024-04-02T00:00:00+00:00"}) == "2024-04-02"


def test_ar_och_nummer_ur_titeln():
    assert calibre_match.ar_och_nummer("2011-2 - Drömmare Nördar") == (2011, 2)
    # Utan bindestreck före temat, som en del av posterna är namngivna.
    assert calibre_match.ar_och_nummer("2023-1 Ylle") == (2023, 1)
    assert calibre_match.ar_och_nummer("Just for the Summer") is None
    assert calibre_match.ar_och_nummer("2024") is None


@pytest.fixture
def gamla_bocker(monkeypatch):
    """Böcker utan utgivningsdatum, namngivna med år och nummer."""
    bocker = {
        1067: _bok("2014-1 - Färg", "Hemslöjd", ""),
        1065: _bok("2014-2 - Eldsjälar", "Hemslöjd", ""),
        1066: _bok("2014-3 - Björk", "Hemslöjd", ""),
    }
    monkeypatch.setattr(
        calibre_api, "fetch_library",
        lambda config, force=False: calibre_api.Bibliotek(bocker, 0.0),
    )
    calibre_match.glom_forslag()
    return bocker


def _ar_2014(session, publication):
    """Hemslöjds sex nummer 2014, med uppmätta utgivningsdatum."""
    datum = ["2014-01-29", "2014-03-26", "2014-05-27",
             "2014-08-13", "2014-10-01", "2014-11-26"]
    for index, dag in enumerate(datum):
        repo.upsert_issue(
            session, publication,
            {"uid": str(index), "name": dag, "publication_date": dag},
        )
    publication.catalogue_complete = True
    session.flush()


def test_bocker_utan_datum_nas_via_ar_och_nummer(db, temp_env, gamla_bocker):
    """Enda vägen till de äldre posterna: de saknar utgivningsdatum helt."""
    with db() as session:
        publication = _publikation(session)
        _ar_2014(session, publication)
        forslag = calibre_match.find_candidates(session, KONFIG)

    traffar = {
        f.issue.publication_date: f.kandidater[0].titel for f in forslag
    }
    assert traffar == {
        "2014-01-29": "2014-1 - Färg",
        "2014-03-26": "2014-2 - Eldsjälar",
        "2014-05-27": "2014-3 - Björk",
    }


def test_ar_och_nummer_kraver_att_utgivaren_stammer(db, temp_env, gamla_bocker):
    """Regression: Sundsvalls femte utgåva 2024 matchade Hemslöjds 2024-5.

    År och nummer är unikt inom en tidning, inte i ett bibliotek med
    flera. Utan författarkravet blev det 46 falska förslag.
    """
    with db() as session:
        publication = _publikation(session, namn="Sundsvalls Tidning")
        _ar_2014(session, publication)
        forslag = calibre_match.find_candidates(session, KONFIG)

    assert forslag == []


def test_lika_stora_filer_ar_inget_att_varna_for():
    """En ersättning skulle inte ändra något, så varningen vore fel besked."""
    assert calibre_match._samma_storlek(16379542, 16379542) is True
    # Calibre skriver in metadata i PDF:en: 59 416 bytes på 15 MB.
    assert calibre_match._samma_storlek(15374128, 15433544) is True
    assert calibre_match._samma_storlek(17307909, 15433544) is False
    # Utan lokal fil finns inget att jämföra.
    assert calibre_match._samma_storlek(None, 15433544) is None
    assert calibre_match._samma_storlek(15433544, None) is None


def test_kandidaten_bar_med_sig_storleksjamforelsen(db, temp_env, bibliotek):
    with db() as session:
        publication = _publikation(session)
        issue, _ = repo.upsert_issue(
            session, publication,
            {"uid": "1", "name": "2024-04-02", "publication_date": "2024-04-02"},
        )
        pdf = temp_env / "output" / "Tidningen Hemslöjd" / "lika.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        # Exakt samma storlek som bok 1541 i fixturen.
        pdf.write_bytes(b"x" * 15433544)
        issue.file_path = str(pdf)
        session.flush()
        forslag = calibre_match.find_candidates(session, KONFIG)

    assert forslag[0].kandidater[0].samma_storlek is True
