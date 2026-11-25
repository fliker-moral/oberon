"""Local diagnostics and durable records for background analysis."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import traceback
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path


logger = logging.getLogger("oberon")
current_run = ContextVar("oberon_run", default=None)


def configure_logs(data_dir: Path) -> None:
    directory = data_dir / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(directory / "oberon.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False


def log_failure(event: str, exc: Exception, *, level: int = logging.ERROR, **fields) -> None:
    frames = " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                        for frame in traceback.extract_tb(exc.__traceback__)[-8:])
    context = " ".join(f"{name}={value}" for name, value in fields.items())
    logger.log(level, "%s %s error_type=%s frames=%s", event, context, type(exc).__name__, frames)


def prepare_runs(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS analysis_runs (
            id TEXT PRIMARY KEY, action TEXT NOT NULL, state TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'collection', message TEXT NOT NULL DEFAULT '',
            result_id INTEGER, dismissed INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS analysis_responses (
            scope TEXT NOT NULL, request_key TEXT NOT NULL, response TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY(scope, request_key)
        );
        CREATE TABLE IF NOT EXISTS analysis_batch_splits (
            scope TEXT NOT NULL, request_key TEXT NOT NULL,
            created_at INTEGER NOT NULL DEFAULT (unixepoch()),
            PRIMARY KEY(scope, request_key)
        );
    """)


def recover_runs(conn: sqlite3.Connection) -> None:
    with conn:
        interrupted = conn.execute("""
            UPDATE analysis_runs SET state='failed',
                message='Oberon закрылся во время анализа. Повторите запуск, чтобы использовать сохранённые ответы.'
            WHERE state='running'
        """).rowcount
        conn.execute("DELETE FROM analysis_responses WHERE created_at < unixepoch() - 7 * 86400")
        conn.execute("DELETE FROM analysis_batch_splits WHERE created_at < unixepoch() - 7 * 86400")
        conn.execute("DELETE FROM analysis_runs WHERE dismissed=1 AND created_at < unixepoch() - 7 * 86400")
    if interrupted:
        logger.warning("runs_interrupted count=%s", interrupted)


def fingerprint(*values) -> str:
    encoded = json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def record_saved(conn: sqlite3.Connection, result_id: int) -> None:
    """Link the report and the background run in the report's own transaction."""
    run = current_run.get()
    if run is not None:
        conn.execute("UPDATE analysis_runs SET state='success',stage='save',result_id=?,message=? WHERE id=?",
                     (result_id, saved_message(run), run["id"]))
        if run.get("scope") and not run.get("saved_partial"):
            conn.execute("DELETE FROM analysis_responses WHERE scope=?", (run["scope"],))
            conn.execute("DELETE FROM analysis_batch_splits WHERE scope=?", (run["scope"],))


def saved_message(run: dict) -> str:
    return "Сохранён частичный коммерческий разбор" if run.get("saved_partial") else "Результат сохранён"
