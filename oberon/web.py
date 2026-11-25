"""Private HTTP service used by the Oberon window and the development preview."""

from __future__ import annotations

import base64
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .core import (
    FetchError, IncompleteResponse, MAX_ZIP_BYTES, ROOT, database_path, db_connect,
    fetch_https, load_env, make_offer, redact, train_snapshot, refine_training,
)
from .diagnostics import configure_logs, current_run, log_failure, logger, recover_runs, saved_message
from .local_git import Repository, git_executable
from .providers import PROVIDERS, TextModel, list_models, provider_catalog
from .pipeline import Cancelled, progress, progress_callback
from .sources import github_diff
from .versions import analyze_version, collect_source, compare_versions, get_version, refresh, source_from_text
from .workspace import (
    USER_ID, change_price, edit_project, export_report, integer, llm,
    manual_project, prepare, project_detail, project_row, projects, save_feedback, save_item,
    save_settings, secret, settings_view, text_field, training_detail, update_feedback,
)

STATIC = ROOT / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/oberon.png": ("oberon.png", "image/png"),
}
MAX_ENCODED_ZIP = ((MAX_ZIP_BYTES + 2) // 3) * 4
MAX_REQUEST_BYTES = MAX_ENCODED_ZIP + 100_000
LONG_ACTIONS = {"analyze", "train", "refresh", "retrain"}


def database_id(value: object) -> int:
    return integer(value, 1, 10**12)


def boot_data(conn: sqlite3.Connection, server: "AppServer") -> dict:
    training = conn.execute(
        """SELECT t.id,t.summary,t.source,t.created_at,
           (SELECT COUNT(*) FROM feedback f WHERE f.training_id=t.id) AS reviews
           FROM training t WHERE t.user_id=? ORDER BY t.id DESC""", (USER_ID,),
    ).fetchall()
    return {
        "settings": settings_view(conn), "projects": projects(conn), "training": [dict(row) for row in training],
        "request_token": server.request_token, "native": bool(server.desktop_token),
        "git_available": bool(git_executable()), "providers": provider_catalog(),
        "pending_job": server.pending_job(conn),
    }


def upload_bytes(data: dict) -> tuple[str, bytes | Path]:
    if "archive_path" in data:
        path = Path(text_field(data, "archive_path", 4000, required=True))
        if not path.is_absolute() or path.suffix.lower() != ".zip" or not path.is_file():
            raise ValueError("Выберите доступный ZIP-архив на диске")
        return path.name, path
    raw_name = text_field(data, "name", 255, required=True).replace("\\", "/")
    filename = raw_name.rsplit("/", 1)[-1]
    if not filename.lower().endswith(".zip"):
        raise ValueError("Выберите ZIP-архив")
    encoded = data.get("content")
    if not isinstance(encoded, str) or len(encoded) > MAX_ENCODED_ZIP:
        raise ValueError("Архив больше 64 МиБ или повреждён")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("Архив повреждён") from exc
    if len(raw) > MAX_ZIP_BYTES:
        raise ValueError("Архив больше 64 МиБ")
    return filename, raw


def parse_source(data: dict) -> tuple[dict, bytes | Path | None]:
    kind = data.get("kind", "auto")
    description = text_field(data, "description", 6000)
    name = text_field(data, "name_override", 150)
    branch = text_field(data, "branch", 120)
    zip_data = None
    if kind == "zip":
        filename, zip_data = upload_bytes(data)
        source = {"kind": "zip", "url": filename, "description": redact(description)}
    elif kind == "local_git":
        path = text_field(data, "path", 1000, required=True)
        source = {"kind": kind, "path": path, "branch": branch or "HEAD", "description": redact(description)}
    elif kind in ("auto", "github", "miniapp"):
        text = text_field(data, "text", 6000, required=True)
        source = source_from_text((text + "\n" + description).strip(), kind)
        if source["kind"] == "github":
            source["branch"] = branch
    else:
        raise ValueError("Выберите источник проекта")
    if name:
        source["name"] = name
    return source, zip_data


def check_connection(conn: sqlite3.Connection, service: str) -> dict:
    if not isinstance(service, str) or (service not in PROVIDERS and service != "github"):
        raise ValueError("Неизвестный сервис")
    provider = PROVIDERS.get(service)
    key = secret(conn, provider.key_field if provider else "github_token")
    if not key:
        raise ValueError("Сохраните ключ в настройках, затем проверьте подключение")
    if service == "qwen":
        settings = settings_view(conn)
        model_id = settings["provider_models"]["qwen"]
        model = TextModel("qwen", key, model_id, 1000, 64, False, qwen_host=settings["qwen_host"])
        model.complete_json('Ответь только JSON: {"ok": true}', {"test": True})
        return {"message": f"Qwen доступен · {dict(provider.models)[model_id]}"}
    if provider is not None:
        models = list_models(service, key, base_url=settings_view(conn)["timeweb_base_url"]) if service == "timeweb" else list_models(service, key)
        if not models:
            raise ValueError(f"{provider.label} не вернул подходящих моделей для этого ключа.")
        return {"message": f"{provider.label} доступен", "models": models}
    host = "api.github.com"
    try:
        raw, _ = fetch_https(
            f"https://{host}/user", max_bytes=320_000, allowed_hosts={host},
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
    except FetchError as exc:
        errors = {401: "GitHub не принял токен", 403: "GitHub ограничил доступ. Проверьте токен и его разрешения."}
        raise ValueError(errors.get(exc.status, str(exc))) from exc
    return {"message": "GitHub доступен", "login": json.loads(raw).get("login", "")}


def analysis_options(conn: sqlite3.Connection) -> dict:
    return {"github_token": secret(conn, "github_token"), "use_feedback": settings_view(conn)["use_feedback"]}


def new_analysis(conn: sqlite3.Connection, data: dict) -> dict:
    model = None if data.get("snapshot_only") is True else llm(conn)
    source, archive = parse_source(data)
    project_id, *_ = analyze_version(conn, USER_ID, source, model, zip_data=archive, **analysis_options(conn))
    return {"id": project_id} if current_run.get() is not None else project_detail(conn, project_id)


def refresh_analysis(conn: sqlite3.Connection, project_id: int, data: dict) -> dict:
    model = None if data.get("snapshot_only") is True else llm(conn)
    options = analysis_options(conn)
    options["repeat"] = data.get("repeat") is True
    if "content" not in data and "archive_path" not in data:
        refresh(conn, USER_ID, project_id, model, **options)
    else:
        filename, archive = upload_bytes(data)
        source = project_detail(conn, project_id)["source"]
        if source.get("kind") != "zip":
            raise ValueError("Загрузить ZIP можно для проекта, созданного из архива")
        source["url"] = filename
        analyze_version(conn, USER_ID, source, model, project_id, archive, **options)
    return {"id": project_id} if current_run.get() is not None else project_detail(conn, project_id)


def new_training(conn: sqlite3.Connection, data: dict) -> dict:
    model = llm(conn)
    source, archive = parse_source(data)
    if source["kind"] not in {"github", "local_git", "zip"}:
        raise ValueError("Для обучения выберите GitHub, локальный Git или ZIP")
    progress("collection", message="Сбор материалов проекта")
    snapshot = collect_source(source, archive, secret(conn, "github_token"))
    label = source.get("url") or snapshot.get("name") or "Локальный Git"
    training_id, _ = train_snapshot(conn, USER_ID, label, snapshot, model, source_spec=source,
                                   use_feedback=settings_view(conn)["use_feedback"])
    return {"id": training_id} if current_run.get() is not None else training_detail(conn, training_id)


def offer_indices(data: dict) -> list[int]:
    indices = data.get("indices")
    if not isinstance(indices, list) or len(indices) > 8:
        raise ValueError("Выберите идеи для коммерческого предложения")
    return [integer(index, 0, 7) for index in indices]


def project_action(conn: sqlite3.Connection, action: str, data: dict) -> dict:
    project_id = database_id(data.get("project_id"))
    if action == "refresh":
        return refresh_analysis(conn, project_id, data)
    if action == "version":
        return get_version(conn, USER_ID, project_id, database_id(data.get("revision_id")))
    if action == "compare":
        return compare_versions(
            conn, USER_ID, project_id, database_id(data.get("old_id")),
            database_id(data.get("new_id")), secret(conn, "github_token"),
        )
    if action == "git-diff":
        source = project_detail(conn, project_id)["source"]
        base = text_field(data, "base", 64, required=True)
        head = text_field(data, "head", 64, required=True)
        if source.get("kind") == "local_git":
            return Repository(source["path"]).diff(base, head)
        if source.get("kind") == "github":
            return github_diff(source["url"], base, head, secret(conn, "github_token"))
        raise ValueError("Сравнение SHA доступно для Git-репозиториев")
    if action == "offer":
        report = json.loads(project_row(conn, project_id)["report"])
        return {"text": make_offer(report, offer_indices(data))}
    if action == "save-offer":
        project_row(conn, project_id)
        body = text_field(data, "text", 30000)
        with conn:
            conn.execute(
                "INSERT INTO offer_drafts(project_id,body) VALUES (?,?) ON CONFLICT(project_id) DO UPDATE SET body=excluded.body",
                (project_id, body),
            )
    elif action == "price":
        change_price(conn, project_id, data)
    elif action == "edit-project":
        edit_project(conn, project_id, data)
    elif action == "task":
        save_item(conn, project_id, data)
    elif action == "delete-task":
        project_row(conn, project_id)
        with conn:
            conn.execute("DELETE FROM work_items WHERE id=? AND project_id=?",
                         (database_id(data.get("id")), project_id))
    else:
        raise LookupError("Страница не найдена")
    return project_detail(conn, project_id)


def change_data(conn: sqlite3.Connection, action: str, data: dict) -> dict:
    if action == "settings":
        return save_settings(conn, data)
    if action == "check":
        return check_connection(conn, data.get("service", ""))
    if action == "analyze":
        return new_analysis(conn, data)
    if action == "manual":
        return project_detail(conn, manual_project(conn, data))
    if action == "train":
        return new_training(conn, data)
    if action == "retrain":
        training_id = refine_training(conn, USER_ID, database_id(data.get("training_id")), llm(conn),
                                      use_feedback=settings_view(conn)["use_feedback"])
        return {"id": training_id} if current_run.get() is not None else training_detail(conn, training_id)
    if action == "training-offer":
        review = training_detail(conn, database_id(data.get("training_id")))
        indices = offer_indices(data)
        ideas = review["ideas"]
        if any(index >= len(ideas) or not all(ideas[index].get(field)
               for field in ("description", "business_value", "success_metric")) for index in indices):
            raise ValueError("Выберите идеи, у которых указаны предложение, польза и метрика.")
        report = {"project_name": review["metadata"].get("project_name") or review["source"], "recommendations": ideas, "currency": ""}
        return {"text": make_offer(report, indices)}
    if action == "feedback":
        training_id = database_id(data.get("training_id"))
        save_feedback(conn, training_id, data)
        return training_detail(conn, training_id)
    if action == "memory":
        training_id = database_id(data.get("training_id"))
        update_feedback(conn, training_id, data)
        return training_detail(conn, training_id)
    if action == "delete":
        item_id = database_id(data.get("id"))
        if data.get("kind") == "project":
            sql = "DELETE FROM projects WHERE id=? AND user_id=?"
        elif data.get("kind") == "training":
            sql = "DELETE FROM training WHERE id=? AND user_id=?"
        else:
            raise ValueError("Неизвестный тип записи")
        with conn:
            conn.execute(sql, (item_id, USER_ID))
        return {"deleted": True}
    return project_action(conn, action, data)


def workspace_backup(conn: sqlite3.Connection) -> bytes:
    with closing(sqlite3.connect(":memory:")) as backup:
        conn.backup(backup)
        backup.execute("PRAGMA secure_delete=ON")
        backup.execute("DELETE FROM app_secrets")
        backup.execute("DELETE FROM analysis_responses")
        backup.execute("DELETE FROM analysis_batch_splits")
        backup.execute("DELETE FROM analysis_runs")
        backup.commit()
        backup.execute("VACUUM")
        return backup.serialize()


class AppServer(ThreadingHTTPServer):
    def __init__(self, port: int = 0, desktop_token: str = ""):
        self.desktop_token = desktop_token
        self.request_token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.jobs_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)

    def job_status(self, job_id: str) -> dict:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if job:
                return {k: v for k, v in job.items() if k not in {"finished_at", "scope"}}
        with closing(db_connect()) as conn:
            row = conn.execute(
                "SELECT id,action,state,stage,message,result_id FROM analysis_runs WHERE id=?", (job_id,),
            ).fetchone()
        return dict(row) if row else {"stage": "waiting"}

    def pending_job(self, conn) -> str | None:
        with self.jobs_lock:
            row = conn.execute(
                "SELECT id FROM analysis_runs WHERE dismissed=0 ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return row["id"] if row else None

    def dismiss_job(self, conn, job_id: str) -> dict:
        with conn:
            conn.execute("UPDATE analysis_runs SET dismissed=1 WHERE id=? AND state!='running'", (job_id,))
        return {"dismissed": True}

    def job_cancel(self, job_id: str) -> dict:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if job and not job.get("finished_at") and job["stage"] != "save":
                job["cancel_requested"] = True
                logger.info("run_cancel_requested id=%s stage=%s", job_id, job["stage"])
                return {"cancel_requested": True}
            return {"cancel_requested": False}

    def run_action(self, conn, action: str, data: dict) -> dict:
        if action not in LONG_ACTIONS:
            return change_data(conn, action, data)
        job = self.register_job(conn, action, data, background=False)
        return self.execute_job(conn, job, data, background=False)

    def register_job(self, conn, action: str, data: dict, *, background: bool) -> dict | None:
        job_id = text_field(data, "job_id", 80, required=background) or secrets.token_hex(16)
        if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", job_id) is None:
            raise ValueError("Неверный идентификатор анализа")
        with self.jobs_lock:
            now = time.monotonic()
            self.jobs = {key: job for key, job in self.jobs.items() if now - job.get("finished_at", now) < 120}
            if background and conn.execute("SELECT 1 FROM analysis_runs WHERE id=?", (job_id,)).fetchone():
                return None
            if any(not job.get("finished_at") for job in self.jobs.values()):
                raise ValueError("Дождитесь окончания текущего анализа или отмените его")
            # Capture the job before returning HTTP 202. The worker owns its own connection.
            if background:
                with conn:
                    conn.execute("UPDATE analysis_runs SET dismissed=1 WHERE dismissed=0 AND state!='running'")
                    conn.execute("INSERT INTO analysis_runs(id,action,state) VALUES (?,?,'running')", (job_id, action))
            job = {"id": job_id, "action": action, "state": "running", "stage": "collection",
                   "done": 0, "total": 0, "message": "Подготовка проекта"}
            self.jobs[job_id] = job
            return job

    def start_job(self, conn, action: str, data: dict) -> dict:
        job = self.register_job(conn, action, data, background=True)
        if job is not None:
            threading.Thread(target=self.background_job, args=(job, data), name="oberon-analysis", daemon=True).start()
        return {"id": text_field(data, "job_id", 80, required=True)}

    def background_job(self, job: dict, data: dict) -> None:
        try:
            with closing(db_connect()) as conn:
                self.execute_job(conn, job, data, background=True)
        except Exception as exc:
            log_failure("worker_error", exc, id=job["id"])
            with self.jobs_lock:
                job.update(state="failed", message=problem_response(exc)[1], finished_at=time.monotonic())

    def execute_job(self, conn, job: dict, data: dict, *, background: bool) -> dict:
        job_id, action = job["id"], job["action"]
        start = time.monotonic()
        source_kind = data.get("kind")
        source_kind = source_kind if source_kind in ("zip", "local_git", "auto", "github", "miniapp") else "saved"
        logger.info("run_started id=%s action=%s source=%s snapshot_only=%s",
                    job_id, action, source_kind, data.get("snapshot_only") is True)
        def update(values):
            with self.jobs_lock:
                if job.get("cancel_requested"):
                    raise Cancelled("Анализ отменён")
                if values["stage"] == "checkpoint":
                    return
                if values["stage"] == "provider":
                    job.update(provider_phase=values["phase"], provider_started_ms=round(time.time() * 1000))
                    return
                stage_changed = job["stage"] != values["stage"]
                job.pop("provider_phase", None)
                job.pop("provider_started_ms", None)
                job.update(values)
            if background and stage_changed:
                with conn:
                    conn.execute("UPDATE analysis_runs SET stage=? WHERE id=?", (values["stage"], job_id))
            logger.info("run_stage id=%s stage=%s done=%s total=%s",
                        job_id, values["stage"], values["done"], values["total"])

        token = progress_callback.set(update)
        run_token = current_run.set(job if background else None)
        try:
            result = change_data(conn, action, data)
            with self.jobs_lock:
                job.update(state="success", result_id=result["id"], message=saved_message(job))
            logger.info("run_saved id=%s action=%s result_id=%s elapsed_ms=%s",
                        job_id, action, result["id"], round((time.monotonic() - start) * 1000))
            return result
        except Exception as exc:
            state = "cancelled" if isinstance(exc, Cancelled) else "failed"
            message = problem_response(exc)[1]
            log_failure("run_" + state, exc, level=logging.INFO if state == "cancelled" else logging.ERROR,
                        id=job_id, action=action, stage=job["stage"], done=job["done"], total=job["total"],
                        elapsed_ms=round((time.monotonic() - start) * 1000))
            if background:
                with conn:
                    conn.execute("""
                        UPDATE analysis_runs SET state=?,message=? WHERE id=? AND state='running'
                    """, (state, message, job_id))
            with self.jobs_lock:
                job.update(state=state, message=message)
            if background:
                return {}
            raise
        finally:
            current_run.reset(run_token)
            progress_callback.reset(token)
            with self.jobs_lock:
                job["finished_at"] = time.monotonic()


def problem_response(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, (ValueError, IncompleteResponse)):
        return 400, redact(str(exc))
    if isinstance(exc, LookupError):
        return 404, "Страница или запись не найдена"
    if isinstance(exc, (OSError, http.client.HTTPException)):
        return 400, "Не удалось получить данные. Проверьте подключение, ссылку или доступ к файлу."
    if isinstance(exc, sqlite3.Error):
        return 500, "Не удалось сохранить или прочитать данные Oberon."
    return 500, "Внутренняя ошибка Oberon. Подробности в журнале."


class Handler(BaseHTTPRequestHandler):
    server: AppServer
    server_version = "Oberon"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format: str, *args) -> None:
        pass

    def send_data(self, status: int, raw: bytes, kind: str, *, filename: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
            "img-src 'self' data:; object-src 'none'",
        )
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        try:
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError) as exc:
            logger.warning("client_disconnected method=%s status=%s error_type=%s",
                           self.command, status, type(exc).__name__)

    def send_json(self, status: int, data: dict | list) -> None:
        self.send_data(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def authorized(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_port}"
        origin = self.headers.get("Origin")
        supplied = self.headers.get("X-Oberon-Desktop", "").encode("utf-8")
        if (
            self.headers.get("Host") != expected
            or (origin and origin != f"http://{expected}")
            or (self.server.desktop_token and not hmac.compare_digest(supplied, self.server.desktop_token.encode()))
        ):
            self.send_json(403, {"error": "Откройте Oberon через ярлык приложения"})
            return False
        return True

    def do_GET(self) -> None:
        if not self.authorized():
            return
        try:
            request = urlsplit(self.path)
            if request.path in ASSETS:
                path, mime = ASSETS[request.path]
                self.send_data(200, (STATIC / path).read_bytes(), mime)
                return
            if request.path == "/api/progress":
                self.send_json(200, self.server.job_status(parse_qs(request.query).get("id", [""])[0]))
                return
            with closing(db_connect()) as conn:
                query = parse_qs(request.query)
                if request.path == "/api/bootstrap":
                    self.send_json(200, boot_data(conn, self.server))
                elif request.path == "/api/models":
                    self.send_json(200, provider_catalog())
                elif request.path == "/api/project":
                    self.send_json(200, project_detail(conn, database_id(query.get("id", [""])[0])))
                elif request.path == "/api/training":
                    self.send_json(200, training_detail(conn, database_id(query.get("id", [""])[0])))
                elif request.path == "/api/export":
                    name, body = export_report(conn, database_id(query.get("id", [""])[0]), query.get("kind", ["report"])[0])
                    self.send_data(200, body.encode("utf-8-sig"), "text/plain; charset=utf-8", filename=name)
                elif request.path == "/api/backup":
                    self.send_data(200, workspace_backup(conn), "application/octet-stream", filename="oberon-backup.sqlite3")
                else:
                    self.send_json(404, {"error": "Страница не найдена"})
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.handle_problem(exc)

    def do_POST(self) -> None:
        if not self.authorized():
            return
        token = self.headers.get("X-Oberon-Token", "").encode("utf-8")
        if (
            not hmac.compare_digest(token, self.server.request_token.encode())
            or self.headers.get_content_type() != "application/json"
        ):
            self.send_json(403, {"error": "Обновите окно Oberon и повторите действие"})
            return
        try:
            size = integer(self.headers.get("Content-Length"), 1, MAX_REQUEST_BYTES)
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Неверный формат запроса")
            action = urlsplit(self.path).path
            if not action.startswith("/api/"):
                raise LookupError
            if "archive_path" in data and not self.server.desktop_token:
                raise ValueError("Для чтения ZIP с диска откройте Oberon через ярлык")
            if action == "/api/cancel":
                self.send_json(200, self.server.job_cancel(text_field(data, "job_id", 80, required=True)))
                return
            action = action.removeprefix("/api/")
            with closing(db_connect()) as conn:
                if action == "dismiss-job":
                    self.send_json(200, self.server.dismiss_job(conn, text_field(data, "job_id", 80, required=True)))
                    return
                if action in LONG_ACTIONS and data.get("background") is True:
                    self.send_json(202, self.server.start_job(conn, action, data))
                    return
                result = self.server.run_action(conn, action, data)
            self.send_json(200, result)
        except (ValueError, LookupError, IncompleteResponse, OSError, http.client.HTTPException, sqlite3.Error) as exc:
            self.handle_problem(exc)
        except Exception as exc:
            log_failure("request_error", exc, method=self.command)
            self.handle_problem(exc)

    def handle_problem(self, exc: Exception) -> None:
        status, message = problem_response(exc)
        self.send_json(status, {"error": message})


def run() -> None:
    load_env()
    desktop = "--desktop" in sys.argv
    token = sys.stdin.readline().strip() if desktop else ""
    if desktop and len(token) < 32:
        raise SystemExit("Desktop authentication failed")
    data_dir = os.environ.get("OBERON_DATA_DIR")
    configure_logs(Path(data_dir).resolve() if data_dir else database_path().parent)
    logger.info("backend_starting pid=%s desktop=%s", os.getpid(), desktop)
    try:
        serve(desktop, token)
    except Exception as exc:
        log_failure("backend_failed", exc)
        raise SystemExit(1) from None
    finally:
        logger.info("backend_stopped pid=%s", os.getpid())


def serve(desktop: bool, token: str) -> None:
    with closing(db_connect()) as conn:
        prepare(conn)
        recover_runs(conn)
    with AppServer(0 if desktop else int(os.environ.get("OBERON_PORT", "8765")), token) as server:
        if desktop:
            print(server.server_port, flush=True)

            def watch_window() -> None:
                sys.stdin.read(1)
                logger.info("window_connection_closed")
                server.shutdown()

            threading.Thread(target=watch_window, daemon=True).start()
        else:
            url = f"http://127.0.0.1:{server.server_port}/"
            print(url, flush=True)
            if os.environ.get("OBERON_OPEN_BROWSER", "1") != "0":
                webbrowser.open(url)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    run()
