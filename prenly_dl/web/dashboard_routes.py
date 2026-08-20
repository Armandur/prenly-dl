"""Rutter för jobb, statistik och bibliotek."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ..config import output_root
from ..db import repository as repo
from ..db.models import JobStatus
from ..db.session import get_session
from .library import group_by_folder, resolve_file, scan_library
from .rendering import back, flash, render

logger = logging.getLogger(__name__)

router = APIRouter()

STATUS_FILTER = [
    ("alla", "Alla"),
    (JobStatus.QUEUED.value, "I kö"),
    (JobStatus.RUNNING.value, "Kör"),
    (JobStatus.DONE.value, "Klara"),
    (JobStatus.ERROR.value, "Fel"),
]


@router.get("/stats/cards", response_class=HTMLResponse)
def stats_cards(request: Request):
    """Statistikkorten, som htmx hämtar om med jämna mellanrum."""
    with get_session() as session:
        stats = repo.dashboard_stats(session)
    return render(request, "stats_cards.html", stats=stats, flashes=[])


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, status: str = "alla"):
    with get_session() as session:
        jobs = repo.list_jobs_filtered(session, status, limit=100)
        counts = repo.job_counts(session)
        publications = {p.id: p for p in repo.list_publications(session)}
        issues = {i.id: i for i in repo.list_issues(session, limit=500)}
    return render(
        request,
        "jobs.html",
        jobs=jobs,
        counts=counts,
        aktivt_filter=status,
        status_filter=STATUS_FILTER,
        publications=publications,
        issues=issues,
    )


@router.get("/jobs/table", response_class=HTMLResponse)
def jobs_table(request: Request, status: str = "alla"):
    """Bara tabellen, för htmx-uppdatering utan att ladda om sidan."""
    with get_session() as session:
        jobs = repo.list_jobs_filtered(session, status, limit=100)
        publications = {p.id: p for p in repo.list_publications(session)}
        issues = {i.id: i for i in repo.list_issues(session, limit=500)}
    return render(
        request,
        "jobs_table.html",
        jobs=jobs,
        aktivt_filter=status,
        publications=publications,
        issues=issues,
        flashes=[],
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    with get_session() as session:
        job = repo.get_job(session, job_id)
        if job is None:
            flash(request, "Jobbet finns inte", "error")
            return back("/jobs")
        publication = (
            repo.get_publication(session, job.publication_id) if job.publication_id else None
        )
        issue = repo.get_issue(session, job.issue_id) if job.issue_id else None
    return render(request, "job_detail.html", job=job, publication=publication, issue=issue)


@router.post("/jobs/poll-all")
def poll_all(request: Request):
    with get_session() as session:
        repo.enqueue_poll(session)
    flash(request, "Katalogkontroll köad för alla aktiva publikationer", "success")
    return back("/jobs")


@router.get("/api/jobs")
def jobs_json():
    with get_session() as session:
        jobs = repo.list_jobs(session, limit=25)
        payload = [
            {
                "id": job.id,
                "type": job.type,
                "status": job.status,
                "progress_done": job.progress_done,
                "progress_total": job.progress_total,
                "message": job.message,
                "error": job.error,
            }
            for job in jobs
        ]
    return JSONResponse(payload)


@router.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    files = scan_library()
    return render(
        request,
        "library.html",
        groups=group_by_folder(files),
        total_files=len(files),
        total_bytes=sum(f.size for f in files),
        output_dir=output_root(),
    )


@router.get("/library/file/{rel_path:path}")
def library_file(rel_path: str):
    target = resolve_file(rel_path)
    if target is None:
        return JSONResponse({"error": "filen finns inte"}, status_code=404)
    return FileResponse(target, media_type="application/pdf", filename=target.name)
