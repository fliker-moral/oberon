"""Read committed source and diffs without checking out or running project code."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import Counter
from pathlib import Path

from .core import redact, source_excerpt, source_path_allowed, source_skip_reason
from .intelligence import diverse_paths

REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9/._-]{0,119}\Z")
SHA = re.compile(r"[0-9a-fA-F]{7,64}\Z")


def git_executable() -> str | None:
    # Resolving through Windows PATH alone also checks the current directory.
    candidate = shutil.which("git", path=os.environ.get("PATH", ""))
    if candidate and Path(candidate).is_absolute() and Path(candidate).suffix.lower() == ".exe":
        return candidate
    standard = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd" / "git.exe"
    return str(standard) if standard.is_file() else None


class Repository:
    def __init__(self, folder: str):
        path = Path(folder)
        if not path.is_absolute() or str(path).startswith("\\\\") or not path.is_dir():
            raise ValueError("Выберите папку Git на локальном диске")
        self.folder = path.resolve()
        self.executable = git_executable()
        if not self.executable:
            raise ValueError("Git для Windows не найден. Установите Git или используйте GitHub/ZIP.")
        inside = self.run("rev-parse", "--is-inside-work-tree", limit=200)
        if inside.strip() != b"true":
            raise ValueError("Выберите папку рабочего Git-репозитория")
        root = self.run("rev-parse", "--show-toplevel", limit=4000).decode("utf-8").strip()
        self.folder = Path(root).resolve()

    def command(self, *arguments: str) -> tuple[list[str], dict]:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_OPTIONAL_LOCKS="0",
            GIT_TERMINAL_PROMPT="0",
            GIT_NO_LAZY_FETCH="1",
            GIT_NO_REPLACE_OBJECTS="1",
        )
        command = [self.executable, "--no-pager", "-c", "core.fsmonitor=false", "-C", str(self.folder), *arguments]
        return command, environment

    def run(self, *arguments: str, limit: int | None = 800_000, truncate: bool = False) -> bytes:
        command, environment = self.command(*arguments)
        try:
            with subprocess.Popen(
                command, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ) as process:
                timer = threading.Timer(60, process.kill)
                timer.start()
                try:
                    output = process.stdout.read(limit + 1) if limit is not None else process.stdout.read()
                    if limit is not None and len(output) > limit:
                        process.kill()
                        if truncate:
                            return output[:limit]
                        raise ValueError("Результат Git превышает лимит. Выберите меньший проект или диапазон.")
                    if process.wait(timeout=2):
                        raise ValueError("Git не прочитал данные. Проверьте папку, ветку или SHA коммита.")
                    return output
                finally:
                    timer.cancel()
        except FileNotFoundError as exc:
            raise ValueError("Исполняемый файл Git не найден") from exc

    def commit(self, reference: str) -> str:
        if not REF.fullmatch(reference):
            raise ValueError("Укажите имя ветки, тега или SHA")
        return self.run("rev-parse", "--verify", "--end-of-options", reference + "^{commit}", limit=100).decode().strip()

    def commits(self, reference: str, count: int = 15) -> list[dict]:
        output = self.run("log", "-z", "-n", str(count), "--format=%H%x00%aI%x00%s", reference, "--")
        values = output.decode("utf-8", errors="replace").removesuffix("\x00").split("\x00")
        return [
            {"sha": values[i], "date": values[i + 1], "message": redact(values[i + 2])[:180]}
            for i in range(0, len(values) - 2, 3)
        ]

    def read_blobs(self, lookup: dict, paths: list[str]):
        command, environment = self.command("cat-file", "--batch")
        with tempfile.TemporaryFile() as requests:
            for path in paths:
                requests.write((lookup[path][0] + "\n").encode("ascii"))
            requests.seek(0)
            with subprocess.Popen(command, env=environment, stdin=requests, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
                try:
                    for path in paths:
                        blob, size = lookup[path]
                        header = process.stdout.readline(256).split()
                        if header != [blob.encode("ascii"), b"blob", str(size).encode("ascii")]:
                            raise ValueError("Git не смог прочитать файл коммита. Проверьте, что объекты репозитория доступны локально.")
                        raw = process.stdout.read(size)
                        if len(raw) != size or process.stdout.read(1) != b"\n":
                            raise ValueError("Git вернул неполный файл. Анализ остановлен.")
                        yield path, blob, size, raw
                    if process.wait(timeout=10) != 0:
                        raise ValueError("Не удалось прочитать Git-коммит целиком")
                finally:
                    if process.poll() is None:
                        process.kill()

    def snapshot(self, branch: str = "HEAD") -> dict:
        from .pipeline import progress
        from contextlib import closing
        sha = self.commit(branch)
        tree = self.run("ls-tree", "-rlz", "--full-tree", sha, limit=None)
        candidates = []
        excluded = Counter()
        for entry in tree.decode("utf-8", errors="replace").split("\x00"):
            if not entry:
                continue
            metadata, _, path = entry.partition("\t")
            mode, kind, blob, size = metadata.split()
            reason = "link" if kind != "blob" or mode not in {"100644", "100755"} else source_skip_reason(path)
            if reason:
                excluded[reason] += 1
                continue
            candidates.append((path, blob, int(size)))
        files = []
        lookup = {path: (blob, size) for path, blob, size in candidates}
        inventory = []
        with closing(self.read_blobs(lookup, diverse_paths(list(lookup)))) as blobs:
            for index, (path, blob, size, raw) in enumerate(blobs, 1):
                progress("collection", index, len(lookup), "Чтение коммита Git")
                excerpt = source_excerpt(path, raw)
                if excerpt is not None:
                    files.append(excerpt)
                    inventory.append({"path": path, "digest": blob, "size": size})
                else:
                    excluded["unsupported_or_binary"] += 1
        if not files:
            raise ValueError("В коммите нет подходящих текстовых файлов")
        return {
            "kind": "local_git",
            "name": self.folder.name,
            "ref": sha,
            "commits": self.commits(sha),
            "files": files,
            "inventory": inventory, "excluded": dict(excluded), "collection_complete": True,
            "limitations": ["Анализируется состояние коммита. Незакоммиченные изменения не входят в снимок.",
                            "Все подходящие текстовые файлы прочитаны целиком. Код и тесты проекта не запускались."],
        }

    def diff(self, base: str, head: str) -> dict:
        if not SHA.fullmatch(base) or not SHA.fullmatch(head):
            raise ValueError("Выберите два SHA коммитов")
        old, new = self.commit(base), self.commit(head)
        if old == new:
            raise ValueError("Выберите разные коммиты")
        options = ("--no-ext-diff", "--no-textconv", "--no-renames")
        entries = self.run("diff", *options, "--name-status", "-z", old, new, "--", limit=None).decode("utf-8", errors="replace")
        pairs = entries.rstrip("\x00").split("\x00")
        count = len(pairs) // 2
        statuses = {"A": "added", "D": "removed", "M": "modified", "T": "modified"}
        files = []
        for i in range(0, count * 2, 2):
            status, path = pairs[i:i + 2]
            if not source_path_allowed(path):
                continue
            raw = self.run("diff", *options, "--no-color", "-U2", old, new, "--", ":(literal)" + path, limit=None)
            files.append({"path": redact(path), "status": statuses.get(status, status),
                          "patch": redact(raw.decode("utf-8", errors="replace"))})
        commits = self.commits(old + ".." + new, count=25)
        total = int(self.run("rev-list", "--count", old + ".." + new, limit=100).strip())
        return {
            "base": old, "head": new, "status": "changed", "total_commits": total,
            "files": files, "commits": commits,
            "limitations": ["Список коммитов показывает последние 25. Сравнение исходников включает все подходящие файлы."],
        }
