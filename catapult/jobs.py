"""In-memory activity/job tracking for Catapult operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any
from uuid import uuid4

from catapult.errors import UNKNOWN, normalize_error, redact_sensitive

DEFAULT_MAX_JOBS = 100
DEFAULT_MAX_EVENTS = 200
VALID_KINDS = {"install", "reinstall", "refresh", "upload", "setup"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_progress(progress: int | float | None) -> int:
    if progress is None:
        return 0
    return max(0, min(100, int(progress)))


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return value[:4000]


@dataclass
class ActivityJob:
    id: str
    kind: str
    title: str
    target: str = ""
    status: str = "running"
    progress: int = 0
    current_message: str = ""
    started_at: str = field(default_factory=_now_iso)
    finished_at: str | None = None
    messages: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    category: str | None = None
    detail: str | None = None

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        def scrub(value: Any) -> Any:
            if not redact:
                return value
            if isinstance(value, str):
                return redact_sensitive(value)
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            return value

        return {
            "id": self.id,
            "kind": self.kind,
            "title": scrub(self.title),
            "target": scrub(self.target),
            "status": self.status,
            "progress": self.progress,
            "message": scrub(self.current_message),
            "current_message": scrub(self.current_message),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "messages": scrub(list(self.messages)),
            "events": scrub(list(self.events)),
            "error": scrub(self.error),
            "error_category": self.category,
            "error_detail": scrub(self.detail),
            "category": self.category,
            "detail": scrub(self.detail),
        }


class ActivityManager:
    """Bounded, process-local store for recent user-visible activity."""

    def __init__(self, *, max_jobs: int = DEFAULT_MAX_JOBS, max_events: int = DEFAULT_MAX_EVENTS):
        self._max_jobs = max_jobs
        self._max_events = max_events
        self._jobs: list[ActivityJob] = []
        self._lock = RLock()

    def start(
        self,
        kind: str,
        title: str,
        *,
        target: str = "",
        message: str = "",
        progress: int = 0,
    ) -> ActivityJob:
        if kind not in VALID_KINDS:
            kind = "refresh" if kind == "auto-refresh" else kind
        if kind not in VALID_KINDS:
            kind = "setup"
        job = ActivityJob(
            id=uuid4().hex,
            kind=kind,
            title=_clean_text(title),
            target=_clean_text(target),
            progress=_clamp_progress(progress),
            current_message=_clean_text(message),
        )
        with self._lock:
            if message:
                job.messages.append(_clean_text(message))
            self._append_event(job, message=message or "Started", status="running")
            self._jobs.insert(0, job)
            del self._jobs[self._max_jobs :]
        return job

    def update(
        self,
        job_or_id: ActivityJob | str,
        *,
        progress: int | None = None,
        message: str | None = None,
        step: str | None = None,
        status: str | None = None,
    ) -> ActivityJob | None:
        with self._lock:
            job = self._find(job_or_id)
            if not job:
                return None
            if progress is not None:
                job.progress = _clamp_progress(progress)
            if status:
                job.status = status
            if message:
                clean = _clean_text(message)
                job.current_message = clean
                job.messages.append(clean)
                self._trim(job.messages)
            self._append_event(job, message=message, step=step, status=job.status)
            return job

    def complete(
        self,
        job_or_id: ActivityJob | str,
        *,
        message: str = "Complete.",
        progress: int = 100,
    ) -> ActivityJob | None:
        with self._lock:
            job = self._find(job_or_id)
            if not job:
                return None
            job.status = "succeeded"
            job.progress = _clamp_progress(progress)
            job.finished_at = _now_iso()
            if message:
                clean = _clean_text(message)
                job.current_message = clean
                job.messages.append(clean)
                self._trim(job.messages)
            self._append_event(job, message=message, status=job.status)
            return job

    def fail(
        self,
        job_or_id: ActivityJob | str,
        error: BaseException | str | None,
        *,
        category: str | None = None,
        detail: str | None = None,
        message: str | None = None,
    ) -> ActivityJob | None:
        normalized = normalize_error(error)
        with self._lock:
            job = self._find(job_or_id)
            if not job:
                return None
            job.status = "failed"
            job.finished_at = _now_iso()
            job.category = category or normalized.category or UNKNOWN
            job.error = _clean_text(message or normalized.message)
            job.detail = _clean_text(detail or normalized.detail)
            job.current_message = job.error or "Failed."
            if job.current_message:
                job.messages.append(job.current_message)
                self._trim(job.messages)
            self._append_event(job, message=job.current_message, status=job.status)
            return job

    def recent(self, *, limit: int = 50, redact: bool = True) -> list[dict[str, Any]]:
        limit = max(1, min(200, limit))
        with self._lock:
            return [job.to_dict(redact=redact) for job in self._jobs[:limit]]

    def _find(self, job_or_id: ActivityJob | str) -> ActivityJob | None:
        job_id = job_or_id.id if isinstance(job_or_id, ActivityJob) else job_or_id
        for job in self._jobs:
            if job.id == job_id:
                return job
        return None

    def _append_event(
        self,
        job: ActivityJob,
        *,
        message: str | None = None,
        step: str | None = None,
        status: str,
    ) -> None:
        event: dict[str, Any] = {
            "id": f"{job.id}-{len(job.events) + 1}",
            "kind": _clean_text(step or status),
            "title": _clean_text((step or status).replace("_", " ").title()),
            "at": _now_iso(),
            "started_at": _now_iso(),
            "finished_at": _now_iso() if status in {"succeeded", "failed"} else None,
            "status": status,
            "progress": job.progress,
        }
        if step:
            event["step"] = _clean_text(step)
        if message:
            event["message"] = _clean_text(message)
        job.events.append(event)
        self._trim(job.events)

    def _trim(self, values: list[Any]) -> None:
        overflow = len(values) - self._max_events
        if overflow > 0:
            del values[:overflow]


job_manager = ActivityManager()
