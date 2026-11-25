"""Application settings, project records and the local work plan."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import sqlite3
from ctypes import wintypes

from .core import ensure_user, redact, report_markdown
from .diagnostics import prepare_runs
from .local_git import git_executable
from .providers import PROVIDERS, TextModel, qwen_api_host, timeweb_provider
from .versions import latest_change, list_versions
from .intelligence import SCOPES

USER_ID = 1
KEY_ENV = {
    "deepseek_key": "DEEPSEEK_API_KEY",
    "gemini_key": "GEMINI_API_KEY",
    "openai_key": "OPENAI_API_KEY",
    "qwen_key": "DASHSCOPE_API_KEY",
    "timeweb_key": "TIMEWEB_AI_API_KEY",
    "github_token": "GITHUB_TOKEN",
}
TASK_STATUSES = {"todo": "Запланировано", "doing": "В работе", "done": "Готово"}


class DataBlob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(raw: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise ValueError("Сохранение ключей в приложении доступно в Windows")
    source = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    incoming = DataBlob(len(raw), source)
    outgoing = DataBlob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    action = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    action.argtypes = [
        ctypes.POINTER(DataBlob), wintypes.LPCWSTR if protect else ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob),
    ]
    action.restype = wintypes.BOOL
    if not action(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ValueError("Windows не смогла " + ("сохранить" if protect else "прочитать") + " ключ")
    try:
        return ctypes.string_at(outgoing.data, outgoing.length)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.data, wintypes.HLOCAL))


def encrypted(value: str) -> str:
    return "dpapi:" + base64.b64encode(dpapi(value.encode(), protect=True)).decode("ascii")


def prepare(conn: sqlite3.Connection) -> None:
    ensure_user(conn, USER_ID)
    prepare_runs(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS app_secrets (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS work_items (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
      title TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN ('todo','doing','done')),
      origin_key TEXT,
      UNIQUE(project_id, origin_key)
    );
    CREATE TABLE IF NOT EXISTS offer_drafts (
      project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
      body TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS training_feedback_idx ON feedback(user_id, training_id, idea_index);
    """)
    with conn:
        for row in conn.execute("SELECT key,value FROM app_secrets WHERE value NOT LIKE 'dpapi:%'"):
            if row["value"]:
                conn.execute("UPDATE app_secrets SET value=? WHERE key=?", (encrypted(row["value"]), row["key"]))


def secret(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM app_secrets WHERE key=?", (key,)).fetchone()
    if row and row["value"]:
        return dpapi(base64.b64decode(row["value"].removeprefix("dpapi:")), protect=False).decode()
    return os.environ.get(KEY_ENV[key], "").strip()


def setting(conn: sqlite3.Connection, key: str, fallback: str) -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else fallback


def key_state(conn: sqlite3.Connection, key: str) -> str:
    if conn.execute("SELECT 1 FROM app_secrets WHERE key=?", (key,)).fetchone():
        return "stored"
    return "env" if os.environ.get(KEY_ENV[key], "").strip() else "missing"


def model_setting(provider: str) -> str:
    # Keep the key already stored by 3.1 for DeepSeek.
    return "model" if provider == "deepseek" else "model_" + provider


def settings_view(conn: sqlite3.Connection) -> dict:
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (USER_ID,)).fetchone()
    provider = setting(conn, "provider", "deepseek")
    if provider not in PROVIDERS:
        provider = "deepseek"
    provider_models = {
        name: setting(conn, model_setting(name), spec.models[0][0])
        for name, spec in PROVIDERS.items()
    }
    return {
        "provider": provider,
        "model": provider_models[provider],
        "provider_models": provider_models,
        "qwen_host": setting(conn, "qwen_host", ""),
        "timeweb_base_url": setting(conn, "timeweb_base_url", ""),
        "use_feedback": setting(conn, "use_feedback", "1") == "1",
        "thinking": setting(conn, "thinking", "0") == "1",
        "max_input_chars": int(setting(conn, "max_input_chars", "24000")),
        "max_output_tokens": int(setting(conn, "max_output_tokens", "5000")),
        "hourly_rate": user["hourly_rate"],
        "minimum_price": user["minimum_price"],
        "currency": user["currency"],
        **{name: key_state(conn, spec.key_field) for name, spec in PROVIDERS.items()},
        "github": key_state(conn, "github_token"),
    }


def text_field(data: dict, key: str, limit: int, *, required: bool = False) -> str:
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"Проверьте длину поля (до {limit} символов)")
    value = value.strip()
    if required and not value:
        raise ValueError("Заполните обязательные поля")
    return value


def integer(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or (isinstance(value, str) and not re.fullmatch(r"\d{1,12}", value)):
        raise ValueError("Введите целое число")
    result = int(value)
    if not low <= result <= high:
        raise ValueError(f"Число должно быть от {low} до {high}")
    return result


def save_settings(conn: sqlite3.Connection, data: dict) -> dict:
    saved = settings_view(conn)
    provider = data.get("provider", saved["provider"])
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise ValueError("Выберите провайдера модели")
    model = data.get("model", saved["provider_models"][provider])
    rate = integer(data.get("hourly_rate", saved["hourly_rate"]), 1, 1_000_000)
    minimum = integer(data.get("minimum_price", saved["minimum_price"]), 0, 1_000_000_000)
    currency = text_field({"currency": data.get("currency", saved["currency"])}, "currency", 8, required=True)
    if not PROVIDERS[provider].allows(model):
        raise ValueError("Проверьте выбранную модель")
    timeweb_url = text_field(data, "timeweb_base_url", 250) if "timeweb_base_url" in data else saved["timeweb_base_url"]
    if timeweb_url or provider == "timeweb":
        timeweb_url = timeweb_provider(timeweb_url).base_url
    fields = {
        "provider": provider,
        model_setting(provider): model,
        "timeweb_base_url": timeweb_url,
        "qwen_host": qwen_api_host(text_field(data, "qwen_host", 250)) if "qwen_host" in data else saved["qwen_host"],
        "use_feedback": str(int(data.get("use_feedback", saved["use_feedback"]) is True)),
        "thinking": str(int(data.get("thinking", saved["thinking"]) is True)),
        "max_input_chars": str(integer(data.get("max_input_chars", saved["max_input_chars"]), 8000, 64000)),
        "max_output_tokens": str(integer(data.get("max_output_tokens", saved["max_output_tokens"]), 1500, 32000)),
    }
    with conn:
        conn.executemany(
            "INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            fields.items(),
        )
        conn.execute("UPDATE users SET hourly_rate=?,minimum_price=?,currency=? WHERE user_id=?", (rate, minimum, currency, USER_ID))
        for key in KEY_ENV:
            if data.get("clear_" + key) is True:
                conn.execute("DELETE FROM app_secrets WHERE key=?", (key,))
                continue
            value = text_field(data, key, 300)
            if value:
                if not re.fullmatch(r"[!-~]{8,300}", value):
                    raise ValueError("Проверьте формат API-ключа")
                conn.execute(
                    "INSERT INTO app_secrets(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, encrypted(value)),
                )
    return settings_view(conn)


def llm(conn: sqlite3.Connection) -> TextModel:
    settings = settings_view(conn)
    provider = settings["provider"]
    spec = PROVIDERS[provider]
    key = secret(conn, spec.key_field)
    if not key:
        raise ValueError(f"Добавьте ключ {spec.label} в настройках.")
    return TextModel(provider, key, settings["model"], settings["max_input_chars"],
                     settings["max_output_tokens"], settings["thinking"], qwen_host=settings["qwen_host"],
                     timeweb_base_url=settings["timeweb_base_url"])


def projects(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT p.id,p.name,p.created_at,p.report,
            (SELECT COUNT(*) FROM project_revisions r WHERE r.project_id=p.id) AS revisions,
            (SELECT COUNT(*) FROM work_items w WHERE w.project_id=p.id AND w.status!='done') AS pending
        FROM projects p WHERE p.user_id=? ORDER BY p.id DESC
        """, (USER_ID,)).fetchall()
    return [
        {"id": row["id"], "name": row["name"], "created_at": row["created_at"], "revisions": row["revisions"],
         "pending": row["pending"], "kind": json.loads(row["report"]).get("source_kind", "text")}
        for row in rows
    ]


def project_row(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, USER_ID)).fetchone()
    if not row:
        raise ValueError("Проект не найден")
    return row


def project_detail(conn: sqlite3.Connection, project_id: int) -> dict:
    row = project_row(conn, project_id)
    revision = conn.execute(
        "SELECT source,observations FROM project_revisions WHERE project_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
        (project_id, USER_ID),
    ).fetchone()
    offer = conn.execute("SELECT body FROM offer_drafts WHERE project_id=?", (project_id,)).fetchone()
    items = conn.execute(
        "SELECT id,title,details,status FROM work_items WHERE project_id=? ORDER BY id DESC", (project_id,),
    ).fetchall()
    snapshot = json.loads(revision["observations"]) if revision else {}
    return {
        "id": project_id, "name": row["name"], "created_at": row["created_at"],
        "description": row["input_text"],
        "report": json.loads(row["report"]), "source": json.loads(revision["source"]) if revision else {},
        "versions": list_versions(conn, USER_ID, project_id), "commits": snapshot.get("commits", []),
        "changes": latest_change(conn, USER_ID, project_id) if revision else None,
        "items": [dict(item) for item in items], "offer": offer["body"] if offer else "",
    }


def manual_project(conn: sqlite3.Connection, data: dict) -> int:
    name = text_field(data, "name", 150, required=True)
    description = redact(text_field(data, "description", 6000))
    report = {
        "project_name": name, "project_type": "Черновик", "summary": description,
        "source_kind": "manual", "existing_features": [], "recommendations": [], "limitations": [],
        "currency": settings_view(conn)["currency"],
    }
    with conn:
        cursor = conn.execute(
            "INSERT INTO projects(user_id,name,input_text,report) VALUES (?,?,?,?)",
            (USER_ID, name, description, json.dumps(report, ensure_ascii=False)),
        )
        return cursor.lastrowid


def edit_project(conn: sqlite3.Connection, project_id: int, data: dict) -> None:
    name = text_field(data, "name", 150, required=True)
    description = redact(text_field(data, "description", 6000))
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = project_row(conn, project_id)
        report = json.loads(row["report"])
        report["project_name"] = name
        if report.get("source_kind") == "manual":
            report["summary"] = description
        conn.execute(
            "UPDATE projects SET name=?,input_text=?,report=? WHERE id=? AND user_id=?",
            (name, description, json.dumps(report, ensure_ascii=False), project_id, USER_ID),
        )
        latest = conn.execute(
            "SELECT id,source FROM project_revisions WHERE project_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (project_id, USER_ID),
        ).fetchone()
        if latest:
            source = json.loads(latest["source"])
            source.update(name=name, description=description)
            conn.execute(
                "UPDATE project_revisions SET source=? WHERE id=?",
                (json.dumps(source, ensure_ascii=False), latest["id"]),
            )


def change_price(conn: sqlite3.Connection, project_id: int, data: dict) -> None:
    index = integer(data.get("index"), 0, 7)
    low = integer(data.get("cost_min"), 0, 1_000_000_000)
    high = integer(data.get("cost_max"), low, 1_000_000_000)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        report = json.loads(project_row(conn, project_id)["report"])
        if index >= len(report["recommendations"]) or data.get("version_ref") != report.get("version_ref"):
            raise ValueError("Отчёт обновился. Откройте текущую версию и повторите правку.")
        report["recommendations"][index].update(cost_min=low, cost_max=high)
        conn.execute("UPDATE projects SET report=? WHERE id=? AND user_id=?",
                     (json.dumps(report, ensure_ascii=False), project_id, USER_ID))


def save_item(conn: sqlite3.Connection, project_id: int, data: dict) -> None:
    project = project_row(conn, project_id)
    if "recommendation" in data:
        index = integer(data["recommendation"], 0, 7)
        report = json.loads(project["report"])
        if index >= len(report["recommendations"]) or data.get("version_ref") != report.get("version_ref"):
            raise ValueError("Рекомендация изменилась. Откройте текущий отчёт.")
        rec = report["recommendations"][index]
        title, details = rec["title"], rec["description"]
        details += "\n" + "\n".join("• " + task for task in rec.get("tasks", []))
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO work_items(project_id,title,details,origin_key) VALUES (?,?,?,?)",
                (project_id, title, details.strip(), title.casefold()),
            )
        return
    title = text_field(data, "title", 160, required=True)
    details = text_field(data, "details", 3000)
    status = data.get("status", "todo")
    if not isinstance(status, str) or status not in TASK_STATUSES:
        raise ValueError("Выберите статус задачи")
    with conn:
        if data.get("id"):
            item_id = integer(data["id"], 1, 10**12)
            result = conn.execute(
                "UPDATE work_items SET title=?,details=?,status=? WHERE id=? AND project_id=?",
                (title, details, status, item_id, project_id),
            )
            if not result.rowcount:
                raise ValueError("Задача не найдена")
        else:
            conn.execute(
                "INSERT INTO work_items(project_id,title,details,status) VALUES (?,?,?,?)",
                (project_id, title, details, status),
            )


def export_report(conn: sqlite3.Connection, project_id: int, kind: str) -> tuple[str, str]:
    detail = project_detail(conn, project_id)
    if kind == "offer":
        if not detail["offer"]:
            raise ValueError("Сохраните черновик КП перед экспортом")
        return f"oberon-{project_id}-offer.txt", detail["offer"]
    contents = report_markdown(detail["report"])
    if detail["items"]:
        contents += "\n\n## План работ\n"
        for item in detail["items"]:
            contents += f"\n- {item['title']} ({TASK_STATUSES[item['status']]})"
    return f"oberon-{project_id}-report.md", contents


def training_detail(conn: sqlite3.Connection, training_id: int) -> dict:
    row = conn.execute("SELECT * FROM training WHERE id=? AND user_id=?", (training_id, USER_ID)).fetchone()
    if not row:
        raise ValueError("Разбор не найден")
    feedback = conn.execute(
        "SELECT id,idea_index,verdict,comment,scope,enabled,rating,correction FROM feedback WHERE training_id=? AND user_id=? ORDER BY id DESC",
        (training_id, USER_ID),
    ).fetchall()
    return {
        "id": row["id"], "summary": row["summary"], "source": row["source"],
        "ideas": json.loads(row["ideas"]), "feedback": [dict(item) for item in feedback],
        "metadata": json.loads(row["metadata"]),
    }


def save_feedback(conn: sqlite3.Connection, training_id: int, data: dict) -> None:
    training = training_detail(conn, training_id)
    comment = redact(text_field(data, "comment", 1000))
    correction = redact(text_field(data, "correction", 1500))
    verdict = data.get("verdict")
    rating = integer(data["rating"], 1, 5) if "rating" in data and verdict != "own" else None
    if rating:
        verdict = "good" if rating >= 4 else "bad"
    if verdict not in ("good", "bad", "own") or (rating is None and len(comment) < 3):
        raise ValueError("Добавьте оценку и пояснение")
    index = -1 if verdict == "own" else integer(data.get("idea_index"), 0, len(training["ideas"]) - 1)
    scope = data.get("scope", "similar")
    if not isinstance(scope, str) or scope not in SCOPES:
        raise ValueError("Выберите область применения оценки")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id FROM feedback WHERE user_id=? AND training_id=? AND idea_index=? ORDER BY id DESC LIMIT 1",
            (USER_ID, training_id, index),
        ).fetchone() if index >= 0 else None
        if existing:
            conn.execute("UPDATE feedback SET verdict=?,comment=?,scope=?,rating=?,correction=?,enabled=1 WHERE id=? AND user_id=?",
                         (verdict, comment, scope, rating, correction, existing["id"], USER_ID))
            return
        conn.execute(
            "INSERT INTO feedback(training_id,user_id,idea_index,verdict,comment,scope,rating,correction) VALUES (?,?,?,?,?,?,?,?)",
            (training_id, USER_ID, index, verdict, comment, scope, rating, correction),
        )


def update_feedback(conn: sqlite3.Connection, training_id: int, data: dict) -> None:
    training_detail(conn, training_id)
    feedback_id = integer(data.get("id"), 1, 10**12)
    row = conn.execute("SELECT * FROM feedback WHERE id=? AND training_id=? AND user_id=?",
                       (feedback_id, training_id, USER_ID)).fetchone()
    if not row:
        raise ValueError("Оценка не найдена")
    operation = data.get("operation")
    with conn:
        if operation == "delete":
            conn.execute("DELETE FROM feedback WHERE id=? AND user_id=?", (feedback_id, USER_ID))
        elif operation == "toggle":
            conn.execute("UPDATE feedback SET enabled=? WHERE id=? AND user_id=?", (int(not row["enabled"]), feedback_id, USER_ID))
        elif operation == "edit":
            comment = redact(text_field(data, "comment", 1000))
            correction = redact(text_field(data, "correction", 1500)) if "correction" in data else row["correction"]
            scope = data.get("scope")
            if (row["rating"] is None and len(comment) < 3) or not isinstance(scope, str) or scope not in SCOPES:
                raise ValueError("Добавьте объяснение и область применения")
            conn.execute("UPDATE feedback SET comment=?,scope=?,correction=? WHERE id=? AND user_id=?", (comment, scope, correction, feedback_id, USER_ID))
        else:
            raise ValueError("Неизвестное действие с памятью")
