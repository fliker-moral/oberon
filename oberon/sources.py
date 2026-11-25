"""Bounded collectors for public GitHub repositories and Mini App frontends."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import urllib.parse
from pathlib import Path
from html.parser import HTMLParser

from .core import FetchError, MAX_ZIP_BYTES, collect_site, fetch_https, inspect_zip, redact, source_path_allowed


GITHUB_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
SHA = re.compile(r"[0-9a-fA-F]{7,40}\Z")


def github_parts(url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"} or parsed.username or parsed.password or parsed.port:
        raise ValueError("Нужна публичная HTTPS-ссылка вида https://github.com/owner/repo")
    segments = parsed.path.strip("/").split("/")
    if len(segments) < 2 or not all(GITHUB_NAME.fullmatch(s) and s not in {".", ".."} for s in segments[:2]):
        raise ValueError("Нужна ссылка вида https://github.com/owner/repo")
    repo = segments[1].removesuffix(".git")
    if not repo or repo in {".", ".."}:
        raise ValueError("Укажите имя репозитория")
    if len(segments) > 2:
        raise ValueError("Укажите главную страницу репозитория. Ветка задаётся отдельно.")
    return segments[0], repo


def github_json(owner: str, repo: str, suffix: str, token: str = "") -> dict | list:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        data, _ = fetch_https(
            f"https://api.github.com/repos/{owner}/{repo}{suffix}", max_bytes=2_500_000,
            headers=headers, allowed_hosts={"api.github.com"},
        )
    except FetchError as exc:
        if exc.status == 404:
            raise ValueError("GitHub не нашёл репозиторий, ветку или коммиты. Для приватного проекта проверьте доступ токена.") from exc
        if exc.status == 401:
            raise ValueError("GitHub не принял токен. Проверьте настройки.") from exc
        if exc.status in {403, 429}:
            raise ValueError("GitHub ограничил доступ или частоту запросов.") from exc
        raise
    return json.loads(data)


def commit_message(value: object) -> str:
    return redact(str(value or "").partition("\n")[0])[:180] or "Без описания"


def github_commits(url: str, limit: int = 15, token: str = "", branch: str = "") -> list[dict]:
    owner, repo = github_parts(url)
    params = {"per_page": str(min(limit, 20))}
    if branch:
        params["sha"] = branch
    rows = github_json(owner, repo, "/commits?" + urllib.parse.urlencode(params), token)
    if not isinstance(rows, list):
        raise ValueError("Не удалось получить список коммитов")
    return [{
        "sha": str(r.get("sha", "")),
        "message": commit_message(r.get("commit", {}).get("message")),
        "date": str((r.get("commit", {}).get("author") or {}).get("date") or ""),
    } for r in rows]


def github_snapshot(url: str, token: str = "", branch: str = "") -> dict:
    owner, repo = github_parts(url)
    if branch and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/._-]{0,119}", branch):
        raise ValueError("Проверьте имя ветки, тега или SHA")
    metadata = github_json(owner, repo, "", token)
    if not isinstance(metadata, dict) or (metadata.get("private") is True and not token):
        raise ValueError("Для приватного репозитория нужен GitHub token в настройках")
    commits = github_commits(url, token=token, branch=branch)
    if not commits or not re.fullmatch(r"[0-9a-f]{40}", commits[0]["sha"]):
        raise ValueError("Не удалось определить текущий коммит")
    sha = commits[0]["sha"]
    if metadata.get("private"):
        archive_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{sha}"
        data, final = fetch_https(archive_url, max_bytes=MAX_ZIP_BYTES, headers={"Authorization": f"Bearer {token}"}, allowed_hosts={"api.github.com", "codeload.github.com"})
    else:
        archive_url = f"https://codeload.github.com/{owner}/{repo}/zip/{sha}"
        data, final = fetch_https(archive_url, max_bytes=MAX_ZIP_BYTES, allowed_hosts={"codeload.github.com"})
    inspected = inspect_zip(data)
    return {
        "kind": "github", "name": repo, "url": f"https://github.com/{owner}/{repo}",
        "ref": sha, "commits": commits, **inspected,
    }


def github_diff(url: str, base: str, head: str, token: str = "") -> dict:
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        raise ValueError("Нужны SHA коммитов (7–40 шестнадцатеричных символов)")
    owner, repo = github_parts(url)
    if base.lower() == head.lower():
        raise ValueError("Выберите разные коммиты")
    result = github_json(owner, repo, f"/compare/{base}...{head}", token)
    if not isinstance(result, dict):
        raise ValueError("Не удалось получить diff")
    files = []
    for item in result.get("files", [])[:50]:
        path = str(item.get("filename", ""))
        files.append({
            "path": redact(path)[:300], "status": str(item.get("status", ""))[:30],
            "additions": int(item.get("additions", 0)), "deletions": int(item.get("deletions", 0)),
            "patch": redact(str(item.get("patch") or ""))[:1400] if source_path_allowed(path) else "",
        })
    commits = [{
        "sha": str(c.get("sha", "")), "message": commit_message(c.get("commit", {}).get("message")),
    } for c in result.get("commits", [])[:30]]
    return {
        "base": base, "head": head, "status": result.get("status", ""),
        "ahead_by": int(result.get("ahead_by", 0)), "behind_by": int(result.get("behind_by", 0)),
        "total_commits": int(result.get("total_commits", 0)), "files": files, "commits": commits,
        "limitations": ["GitHub показывает изменения от общей базы до нового коммита. До 50 файлов и 30 коммитов."],
    }


class MiniParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts: list[str] = []
        self.buttons: list[str] = []
        self.forms = 0
        self.viewport = False
        self.tags: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])
        if tag == "button":
            self.buttons.append(str(attrs.get("aria-label") or attrs.get("id") or "button")[:80])
        if tag == "form":
            self.forms += 1
        if tag == "meta" and (attrs.get("name") or "").lower() == "viewport":
            self.viewport = True


def inspect_miniapp(url: str) -> dict:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Нужна прямая публичная HTTPS-ссылка на Mini App")
    if parsed.hostname in {"t.me", "telegram.me"}:
        raise ValueError("Ссылка t.me запускает Mini App внутри Telegram. Пришлите прямой URL фронтенда или исходники; t.me не раскрывает страницы приложения")
    raw, final = fetch_https(url, mime_types={"text/html", "application/xhtml+xml"}, timeout=8)
    source = raw.decode("utf-8", errors="replace")
    parser = MiniParser()
    parser.feed(source)
    js_signals = []
    route_candidates = []
    fetched_assets = []
    asset_digests = []
    for src in parser.scripts[:4]:
        asset = urllib.parse.urljoin(final, src)
        parts = urllib.parse.urlsplit(asset)
        if parts.scheme != "https" or parts.hostname != urllib.parse.urlsplit(final).hostname or not parts.path.endswith(".js"):
            continue
        try:
            content, _ = fetch_https(asset, max_bytes=150_000, allowed_hosts={parts.hostname}, timeout=6)
            text = content.decode("utf-8", errors="replace")
            fetched_assets.append(asset)
            asset_digests.append(hashlib.sha256(content).hexdigest())
            for signal in ("Telegram.WebApp", "initData", "MainButton", "BackButton", "CloudStorage", "openInvoice", "sendData"):
                if signal in text and signal not in js_signals:
                    js_signals.append(signal)
            for route in re.findall(r"[\"'](/(?:[A-Za-z0-9_/-]{2,80}))[\"']", text)[:30]:
                if route not in route_candidates and not route.startswith(("/api/", "/assets/")):
                    route_candidates.append(route)
        except (ValueError, OSError, http.client.HTTPException):
            continue
        if len(fetched_assets) >= 3:
            break
    pages = collect_site(final)
    fingerprint = hashlib.sha256(json.dumps({
        "pages": pages["pages"], "scripts": asset_digests,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return {"kind": "miniapp", "url": url, "ref": fingerprint, "pages": pages["pages"], "signals": {"telegram_sdk_script": any("telegram-web-app.js" in x for x in parser.scripts), "js_signals": js_signals, "possible_routes": route_candidates[:20], "viewport_meta": parser.viewport, "forms_on_first_page": parser.forms, "buttons_on_first_page": parser.buttons[:20], "checked_js_assets": fetched_assets}, "limitations": pages["limitations"] + ["Telegram-авторизация, закрытые экраны и реальные платежи не проверялись; JS-код не выполнялся; маршруты из JS являются предположениями"]}


def zip_snapshot(data: bytes | Path, source: str) -> dict:
    inspected = inspect_zip(data)
    files = sorted(inspected["files"], key=lambda file: file["path"])
    manifest = [(file["path"], file["crc"], file["size"]) for file in inspected["inventory"]]
    fingerprint = hashlib.sha256(json.dumps(manifest, ensure_ascii=False).encode()).hexdigest()[:16]
    return {
        "kind": "zip", "url": source, "ref": fingerprint,
        **inspected, "files": files,
    }
