"""Bakgrundsschemaläggare och jobbarbetare.

Körs i samma process som webbtjänsten: en arbetare betar jobbkön ett jobb
i taget, ett cron-jobb köar en katalogkontroll enligt schema, och ett
städjobb rensar gammal jobbhistorik.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import timezone_name
from .covers import cover_filename, purge_unused_covers
from .db import repository as repo
from .db.session import get_session
from .services import execute_job

logger = logging.getLogger(__name__)

POLL_CRON_KEY = "poll_cron"
DEFAULT_POLL_CRON = "0 6 * * *"
JOB_HISTORY_DAYS_KEY = "job_history_days"
DEFAULT_JOB_HISTORY_DAYS = 30

_POLL_JOB_ID = "poll-all"
_WORKER_JOB_ID = "worker"
_PURGE_JOB_ID = "purge"

_scheduler: BackgroundScheduler | None = None


def parse_cron(expression: str) -> CronTrigger:
    """Tolka ett cron-uttryck med fem fält. Kastar ValueError vid fel."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Cron-uttrycket ska ha fem fält, t.ex. 0 6 * * *")
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone=timezone_name(),
    )


def poll_cron_expression() -> str:
    with get_session() as session:
        return repo.get_setting(session, POLL_CRON_KEY, DEFAULT_POLL_CRON)


def run_worker_tick() -> None:
    """Plocka och kör ett jobb ur kön."""
    with get_session() as session:
        job = repo.claim_next_job(session)
        if job is None:
            return
        session.commit()
        logger.info("Kör jobb %s (%s)", job.id, job.type)
        execute_job(session, job)


def queue_scheduled_poll() -> None:
    """Köa dygnets katalogkontroll och en synk per sajt.

    Pollen gäller de bevakade publikationerna. Sajtsynken håller resten av
    katalogen aktuell - nya titlar och färska omslag - så att den som tittar
    ser rätt utan att be om det.
    """
    with get_session() as session:
        repo.enqueue_poll(session)
        sajter = [site.id for site in repo.list_sites(session)]
        for site_id in sajter:
            repo.enqueue_site_sync(session, site_id)
    logger.info("Schemalagd katalogkontroll köad, samt synk av %d sajter", len(sajter))


def purge_history() -> None:
    with get_session() as session:
        days = int(
            repo.get_setting(session, JOB_HISTORY_DAYS_KEY, str(DEFAULT_JOB_HISTORY_DAYS))
            or DEFAULT_JOB_HISTORY_DAYS
        )
        removed = repo.purge_old_jobs(session, days)
        giltiga = {
            cover_filename(publication.title_id, issue.cover_checksum)
            for publication in repo.list_publications(session)
            for issue in repo.list_issues(session, publication.id, limit=1000)
            if issue.cover_checksum
        }
    if removed:
        logger.info("Rensade %d gamla jobb", removed)
    omslag = purge_unused_covers(giltiga)
    if omslag:
        logger.info("Rensade %d omslag som ingen utgåva pekar på", omslag)


def start_scheduler() -> BackgroundScheduler:
    """Starta schemaläggaren och återställ jobb som hängde efter omstart."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    with get_session() as session:
        stale = repo.reset_stale_running_jobs(session)
    if stale:
        logger.warning("Återställde %d jobb som avbröts av omstart", stale)

    scheduler = BackgroundScheduler(timezone=timezone_name())
    scheduler.add_job(
        run_worker_tick,
        "interval",
        seconds=10,
        id=_WORKER_JOB_ID,
        max_instances=1,
        coalesce=True,
    )
    try:
        trigger = parse_cron(poll_cron_expression())
    except ValueError as error:
        logger.warning("Ogiltigt cron-uttryck, faller tillbaka på %s: %s", DEFAULT_POLL_CRON, error)
        trigger = parse_cron(DEFAULT_POLL_CRON)
    scheduler.add_job(queue_scheduled_poll, trigger, id=_POLL_JOB_ID, coalesce=True)
    scheduler.add_job(
        purge_history, CronTrigger(hour=4, minute=30, timezone=timezone_name()),
        id=_PURGE_JOB_ID,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("Schemaläggare igång, katalogkontroll: %s", poll_cron_expression())
    return scheduler


def reschedule_poll(expression: str) -> None:
    """Byt schema för katalogkontrollen. Kastar ValueError vid ogiltigt uttryck."""
    trigger = parse_cron(expression)
    if _scheduler is not None and _scheduler.running:
        _scheduler.reschedule_job(_POLL_JOB_ID, trigger=trigger)
    logger.info("Katalogkontroll schemalagd till %s", expression)


def next_poll_time():
    if _scheduler is None or not _scheduler.running:
        return None
    job = _scheduler.get_job(_POLL_JOB_ID)
    return job.next_run_time if job else None


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
