"""Versioned project analysis and source-aware comparisons."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .core import ANALYSIS_PROMPT, AnalysisModel, ROOT, collect_site, get_user, is_site, redact, user_examples, validate_analysis
from .diagnostics import record_saved
from .sources import github_diff, github_snapshot, inspect_miniapp, zip_snapshot
from .local_git import Repository
from .intelligence import GROUNDING_PROMPT, project_profile
from .pipeline import progress, run_pipeline


def https_urls(text: str) -> list[str]:
    urls = []
    for token in re.findall(r"""https://[^\s<>"']+""", text, re.I):
        parts = urlsplit(token.rstrip(".,;)"))
        if parts.username or parts.password:
            raise ValueError("Уберите логин или пароль из адреса источника")
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if url not in urls:
            urls.append(url)
    return urls


def _text_snapshot(description: str) -> dict:
    urls = [url for url in https_urls(description) if is_site(url)]
    websites = []
    limitations = []
    for url in urls[:2]:
        try:
            websites.append(collect_site(url))
        except (ValueError, OSError) as exc:
            limitations.append(f"{redact(url)}: {exc}")
    if not websites:
        limitations.append("Публичные страницы сайта не проверены")
    digest = hashlib.sha256(json.dumps({
        "description": description,
        "pages": [page for site in websites for page in site["pages"]],
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    return {"kind": "site" if websites else "text", "url": urls[0] if urls else "", "ref": digest, "websites": websites, "limitations": limitations}


def collect_source(source: dict, zip_data: bytes | Path | None = None, github_token: str = "") -> dict:
    kind = source["kind"]
    if kind == "github":
        snapshot = github_snapshot(source["url"], github_token, source.get("branch", ""))
        extra = _text_snapshot(source.get("description", ""))
        snapshot["websites"] = extra["websites"]
        if extra["websites"]:
            snapshot["limitations"].extend(extra["limitations"])
        return snapshot
    if kind == "local_git":
        return Repository(source["path"]).snapshot(source.get("branch") or "HEAD")
    if kind == "miniapp":
        return inspect_miniapp(source["url"])
    if kind == "zip":
        if zip_data is None:
            raise ValueError("Для повторного анализа выберите новую версию ZIP")
        return zip_snapshot(zip_data, source.get("url", "archive.zip"))
    description = source.get("description", "")
    url = source.get("url", "")
    if kind == "site" and url and url not in https_urls(description):
        description += "\n" + url
    return _text_snapshot(description)


def source_from_text(text: str, kind: str = "auto") -> dict:
    text = text.strip()[:6000]
    if len(text) < 12:
        raise ValueError("Пришлите ссылку или описание проекта не короче 12 символов")
    urls = https_urls(text)
    github = next((url for url in urls if urlsplit(url).hostname in {"github.com", "www.github.com"}), None)
    clean_description = redact(re.sub(r"""(https://[^\s<>"'?]+)[?#][^\s<>"']+""", r"\1", text, flags=re.I))
    if kind == "miniapp":
        if not urls:
            raise ValueError("Нужна прямая публичная HTTPS-ссылка на Mini App")
        return {"kind": "miniapp", "url": urls[0], "description": clean_description}
    if kind == "github" and not github:
        raise ValueError("Укажите ссылку https://github.com/owner/repo")
    if github:
        return {"kind": "github", "url": github, "description": clean_description}
    return {"kind": "site" if urls else "text", "url": urls[0] if urls else "", "description": clean_description}


def _file_diff(old: dict, new: dict) -> dict:
    before = {item["path"]: item for item in old.get("files", [])}
    after = {item["path"]: item for item in new.get("files", [])}
    old_index = {f["path"]: f for f in old.get("inventory", old.get("files", []))}
    new_index = {f["path"]: f for f in new.get("inventory", new.get("files", []))}
    files = []
    for path in sorted(before.keys() | after.keys()):
        earlier = before.get(path, {})
        later = after.get(path, {})
        if (not earlier and path in old_index) or (not later and path in new_index):
            continue  # Missing text is a coverage limitation, not a deleted file.
        old_text = earlier.get("content", "")
        new_text = later.get("content", "")
        if earlier and later and earlier.get("digest", old_text) == later.get("digest", new_text):
            continue
        patch = "\n".join(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), n=2, lineterm=""))
        files.append({"path": path, "status": "added" if path not in before else "removed" if path not in after else "modified", "patch": redact(patch)})
    # Inventory detects changes even if a file was outside the text-reading budget.
    known = {f["path"] for f in files}
    for path in sorted(old_index.keys() | new_index.keys()):
        a, b = old_index.get(path), new_index.get(path)
        changed = not a or not b or (a.get("crc") != b.get("crc") if "crc" in a and "crc" in b else a.get("digest") != b.get("digest"))
        if path not in known and changed:
            files.append({"path": path, "status": "added" if path not in old_index else "removed" if path not in new_index else "modified", "patch": ""})
    return {"files": files, "limitations": [] if old.get("collection_complete") and new.get("collection_complete") else ["Старый снимок мог содержать выборку. Сравнение учитывает доступные тексты и индекс."]}


def compare_snapshots(source: dict, previous: dict | None, current: dict, github_token: str = "") -> dict:
    if previous is None:
        return {"status": "initial", "files": [], "limitations": ["Первый снимок проекта: предыдущей версии нет"]}
    if source["kind"] == "github":
        if previous.get("ref") == current.get("ref"):
            return {"status": "site_changed" if previous.get("websites") != current.get("websites") else "unchanged", "files": [], "commits": [], "limitations": ["Git-коммит не изменился; обновились доступные страницы сайта"] if previous.get("websites") != current.get("websites") else []}
        if previous.get("collection_complete") and current.get("collection_complete"):
            result = _file_diff(previous, current)
            result.update(status="changed", base=previous["ref"], head=current["ref"])
            return result
        return github_diff(source["url"], previous["ref"], current["ref"], github_token)
    if source["kind"] == "local_git":
        if previous["ref"] == current["ref"]:
            return {"status": "unchanged", "files": [], "commits": []}
        return Repository(source["path"]).diff(previous["ref"], current["ref"])
    if source["kind"] == "zip":
        result = _file_diff(previous, current)
        result.update(
            status="changed" if result["files"] else "unchanged",
            base=previous["ref"], head=current["ref"],
        )
        return result
    return {"status": "changed" if previous.get("ref") != current.get("ref") else "unchanged", "files": [], "limitations": ["Для сайта сравнивается доступный текст; история Git не подключена"]}


def report_delta(previous: dict | None, current: dict) -> dict:
    if previous is None:
        return {"new_features": [], "removed_features": [], "new_recommendations": [], "removed_recommendations": []}
    def titles(report: dict, key: str) -> dict[str, str]:
        return {item["title"].strip().casefold(): item["title"] for item in report.get(key, []) if isinstance(item, dict) and item.get("title")}
    old_features, new_features = titles(previous, "existing_features"), titles(current, "existing_features")
    old_recs, new_recs = titles(previous, "recommendations"), titles(current, "recommendations")
    return {"new_features": [new_features[k] for k in sorted(new_features.keys() - old_features.keys())], "removed_features": [old_features[k] for k in sorted(old_features.keys() - new_features.keys())], "new_recommendations": [new_recs[k] for k in sorted(new_recs.keys() - old_recs.keys())], "removed_recommendations": [old_recs[k] for k in sorted(old_recs.keys() - new_recs.keys())]}


def analyze_version(
    conn: sqlite3.Connection, user_id: int, source: dict, llm: AnalysisModel | None,
    project_id: int | None = None, zip_data: bytes | Path | None = None, *,
    github_token: str = "", use_feedback: bool = True, repeat: bool = False,
) -> tuple[int, int, dict, dict]:
    user = get_user(conn, user_id)
    previous = None
    previous_report = None
    previous_id = None
    if project_id is not None:
        project = conn.execute("SELECT id FROM projects WHERE id=? AND user_id=?", (project_id, user_id)).fetchone()
        if not project:
            raise ValueError("Проект не найден")
        row = conn.execute("SELECT * FROM project_revisions WHERE project_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (project_id, user_id)).fetchone()
        if row:
            previous_id = row["id"]
            previous = json.loads(row["observations"])
            previous_report = json.loads(row["report"])
    progress("collection", message="Сбор материалов проекта")
    snapshot = collect_source(source, zip_data, github_token)
    first_ai = llm is not None and previous_report and previous_report.get("snapshot_only")
    if not repeat and not first_ai and previous and snapshot["ref"] == previous.get("ref") and snapshot.get("websites") == previous.get("websites"):
        raise ValueError("Источник не изменился. Включите «Повторить анализ» для нового отчёта.")
    changes = compare_snapshots(source, previous, snapshot, github_token)
    if llm is None:
        report = {
            "project_name": snapshot.get("name") or source.get("url") or "Снимок проекта",
            "project_type": "Снимок источника", "summary": source.get("description", ""),
            "limitations": [], "existing_features": [], "recommendations": [],
            "currency": user["currency"], "snapshot_only": True,
        }
    else:
        catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
        examples = user_examples(conn, user_id, project_profile(source, snapshot)) if use_feedback else []
        report, audit = run_pipeline(llm, source, snapshot, changes, previous_report, examples, catalog, conn=conn,
                                     task="analysis", prompt=ANALYSIS_PROMPT + GROUNDING_PROMPT,
                                     validate=lambda raw, evidence: validate_analysis(raw, user, [], evidence))
        if previous_report and not previous_report.get("snapshot_only"):
            changes["analysis_delta"] = report_delta(previous_report, report)
        report["audit"] = audit
        report["limitations"].extend(report["audit"]["limitations"])
    report["limitations"].extend(snapshot.get("limitations", []))
    report["version_ref"] = snapshot["ref"]
    report["source_kind"] = source["kind"]
    report["model"] = getattr(llm, "model", "")
    report["provider"] = getattr(llm, "provider", "")
    report["project_name"] = source.get("name") or report["project_name"]
    serialized = json.dumps(report, ensure_ascii=False)
    progress("save", message="Сохранение отчёта")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if project_id is None:
            cursor = conn.execute(
                "INSERT INTO projects(user_id,name,input_text,report) VALUES (?,?,?,?)",
                (user_id, report["project_name"], source.get("description") or source.get("url") or "Исходники", serialized),
            )
            project_id = cursor.lastrowid
        else:
            row = conn.execute(
                "SELECT MAX(id) AS id FROM project_revisions WHERE project_id=? AND user_id=?",
                (project_id, user_id),
            ).fetchone()
            if row["id"] != previous_id:
                raise ValueError("Появилась более новая версия. Откройте проект и повторите анализ.")
            updated = conn.execute(
                "UPDATE projects SET name=?,report=? WHERE id=? AND user_id=?",
                (report["project_name"], serialized, project_id, user_id),
            )
            if not updated.rowcount:
                raise ValueError("Проект был удалён")
        cursor = conn.execute(
            "INSERT INTO project_revisions(project_id,user_id,source,version_ref,observations,change_data,report) VALUES (?,?,?,?,?,?,?)",
            (project_id, user_id, json.dumps(source, ensure_ascii=False), snapshot["ref"],
             json.dumps(snapshot, ensure_ascii=False), json.dumps(changes, ensure_ascii=False), serialized),
        )
        record_saved(conn, project_id)
        return project_id, cursor.lastrowid, report, changes


def refresh(conn: sqlite3.Connection, user_id: int, project_id: int, llm: AnalysisModel | None, **options) -> tuple[int, int, dict, dict]:
    row = conn.execute("SELECT source FROM project_revisions WHERE project_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (project_id, user_id)).fetchone()
    if row:
        source = json.loads(row["source"])
    else:
        project = conn.execute(
            "SELECT name, input_text FROM projects WHERE id=? AND user_id=?", (project_id, user_id),
        ).fetchone()
        if not project:
            raise ValueError("Проект не найден")
        source = source_from_text(project["input_text"])
        source["name"] = project["name"]
    return analyze_version(conn, user_id, source, llm, project_id, **options)


def list_versions(conn: sqlite3.Connection, user_id: int, project_id: int) -> list[dict]:
    rows = conn.execute("SELECT id,version_ref,created_at,change_data FROM project_revisions WHERE project_id=? AND user_id=? ORDER BY id DESC LIMIT 20", (project_id, user_id)).fetchall()
    return [{"id": row["id"], "ref": row["version_ref"], "created_at": row["created_at"], "change_status": json.loads(row["change_data"]).get("status", "")} for row in rows]


def latest_change(conn: sqlite3.Connection, user_id: int, project_id: int) -> dict:
    row = conn.execute("SELECT change_data FROM project_revisions WHERE project_id=? AND user_id=? ORDER BY id DESC LIMIT 1", (project_id, user_id)).fetchone()
    if not row:
        raise ValueError("Нет сохранённых версий")
    return json.loads(row["change_data"])


def get_version(conn: sqlite3.Connection, user_id: int, project_id: int, revision_id: int) -> dict:
    row = conn.execute("SELECT report FROM project_revisions WHERE id=? AND project_id=? AND user_id=?", (revision_id, project_id, user_id)).fetchone()
    if not row:
        raise ValueError("Версия проекта не найдена")
    return json.loads(row["report"])


def compare_versions(conn: sqlite3.Connection, user_id: int, project_id: int, older_id: int, newer_id: int, github_token: str = "") -> dict:
    rows = conn.execute("SELECT id,source,observations,report,change_data FROM project_revisions WHERE project_id=? AND user_id=? AND id IN (?,?)", (project_id, user_id, older_id, newer_id)).fetchall()
    if len(rows) != 2 or older_id >= newer_id:
        raise ValueError("Укажите две версии одного проекта в порядке от старой к новой")
    rows = {row["id"]: row for row in rows}
    old, new = rows[older_id], rows[newer_id]
    preceding = conn.execute("SELECT id FROM project_revisions WHERE project_id=? AND user_id=? AND id<? ORDER BY id DESC LIMIT 1", (project_id, user_id, newer_id)).fetchone()
    if preceding and preceding["id"] == older_id:
        return json.loads(new["change_data"])
    source = json.loads(new["source"])
    before, after = json.loads(old["observations"]), json.loads(new["observations"])
    try:
        change = compare_snapshots(source, before, after, github_token)
    except (ValueError, OSError):
        if source["kind"] not in {"local_git", "github"}:
            raise
        change = _file_diff(before, after)
        change.update(status="snapshot_only", base=before["ref"], head=after["ref"])
        change["limitations"] = ["Репозиторий недоступен. Сравниваются сохранённые текстовые файлы."]
    old_report, new_report = json.loads(old["report"]), json.loads(new["report"])
    if not old_report.get("snapshot_only") and not new_report.get("snapshot_only"):
        change["analysis_delta"] = report_delta(old_report, new_report)
    return change


def format_change(change: dict, include_patch: bool = False) -> str:
    lines = [f"Сравнение версий: {change.get('base','?')[:12]} → {change.get('head','?')[:12]}", f"Статус: {change.get('status','неизвестен')}; коммитов: {change.get('total_commits', len(change.get('commits', [])))}"]
    if change.get("commits"):
        lines += ["", "Коммиты:"] + [f"• {c['sha'][:10]} {c['message']}" for c in change["commits"][:15]]
    if change.get("files"):
        lines += ["", "Изменённые файлы:"]
        for file in change["files"][:50]:
            counts = f" +{file.get('additions', 0)}/-{file.get('deletions', 0)}" if "additions" in file else ""
            lines.append(f"• {file['status']}: {file['path']}{counts}")
            if include_patch and file.get("patch"):
                lines.append(str(file["patch"])[:1000])
    else:
        lines.append("Изменённых текстовых файлов в доступной выборке нет.")
    if change.get("limitations"):
        lines += ["", "Ограничения: " + "; ".join(change["limitations"])]
    delta = change.get("analysis_delta", {})
    for key, label in (("new_features", "Новые функции"), ("removed_features", "Исчезнувшие функции"), ("new_recommendations", "Новые рекомендации"), ("removed_recommendations", "Снятые рекомендации")):
        if delta.get(key):
            lines += ["", f"{label}: " + ", ".join(delta[key][:15])]
    return "\n".join(lines)
