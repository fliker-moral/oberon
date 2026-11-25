"""Build a local project map and consolidate reviewed evidence for synthesis."""

from __future__ import annotations

import re
from collections import Counter

from .core import source_order
from .intelligence import category


def structure(files: list[dict]) -> dict:
    directories = {}
    seen = set()
    for file in files:
        path = file["path"]
        folder, separator, _ = path.partition("/")
        section = folder + "/" if separator else "Корень проекта"
        details = directories.setdefault(section, {"files": 0, "characters": 0, "categories": Counter(), "paths": []})
        details["characters"] += len(file["content"])
        if path not in seen:
            seen.add(path)
            details["files"] += 1
            details["categories"][category(path)] += 1
            details["paths"].append(path)
    sections = []
    for name, details in sorted(directories.items(), key=lambda item: (-item[1]["files"], item[0])):
        paths = sorted(details.pop("paths"), key=source_order)
        sections.append({"name": name, **details, "examples": paths[:6]})
    return {"sections": sections}


def ranked_findings(reviews: list[dict]):
    """Rotate across batches after comparing evidence within each batch."""
    ranked = []
    for review in reviews:
        findings = [note for note in review["notes"] if "title" in note]
        findings.sort(key=lambda note: (not note["citations"], note["kind"] != "opportunity"))
        ranked.append((review["batch"], findings))
    seen = set()
    for rank in range(max((len(notes) for _, notes in ranked), default=0)):
        for batch, findings in ranked:
            if rank >= len(findings):
                continue
            note = findings[rank]
            key = re.sub(r"[^\w]+", " ", note["title"].casefold()).strip()
            if key and key not in seen:
                seen.add(key)
                yield batch, note


def local_report(task: str, source: dict, snapshot: dict, reviews: list[dict]) -> dict:
    findings = [note for _, note in ranked_findings(reviews)]
    summaries = [note["summary"] for review in reviews for note in review["notes"] if note.get("summary")]
    raw = {
        "project_name": source.get("name") or snapshot.get("name"),
        "summary": " ".join(summaries)[:1100],
        "limitations": ["Итог собран локально из ответов по пакетам. Объём и стоимость работ уточняются."],
        "questions": [note["proposal"] for note in findings if note["kind"] == "question" and note["proposal"]][:5],
    }
    proposals = [{**note, "description": note["proposal"], "evidence": note["observation"]}
                 for note in findings if note["kind"] == "opportunity"]
    proposals.sort(key=lambda note: not note["citations"])
    if task == "training":
        raw["ideas"] = proposals[:6]
    else:
        raw.update(
            project_name=raw["project_name"] or source.get("url"),
            project_type="Разбор по пакетам",
            existing_features=[{**note, "status": "confirmed"} for note in findings
                               if note["kind"] == "feature" and note["citations"]][:12],
            recommendations=proposals[:6],
        )
    return raw
