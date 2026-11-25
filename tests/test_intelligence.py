"""Regression cases for grounding, context coverage and scoped feedback."""

import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from oberon.core import db_connect, inspect_zip, redact, source_excerpt, validate_analysis
from oberon.intelligence import (
    ground_report, select_feedback, verify_citations,
)
from oberon.pipeline import plan, split_text
from oberon.sources import zip_snapshot
from oberon.versions import compare_snapshots


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        for name, text in files.items():
            z.writestr("repo/" + name, text)
    return output.getvalue()


class IntelligenceTests(unittest.TestCase):
    def test_batch_plan_covers_every_file_and_character(self):
        files = {"README.md": "# Магазин\nКод требует проверки", "requirements.txt": "fastapi==0.1\nsqlalchemy>=2\n"}
        files.update({f"modules/module_{i}.py": "def display():\n    return 1\n" * 60 for i in range(15)})
        files["billing.py"] = "# header\n" * 16_000 + 'def deduplicate_payment(payment_id):\n    return storage.find(payment_id)\n'
        files["tests/test_billing.py"] = 'def test_deduplication():\n    assert deduplicate_payment("p1") is not None\n'
        snapshot = {"kind": "zip", "ref": "test", **inspect_zip(archive(files))}
        source = {"kind": "zip", "url": "shop.zip", "description": "Проверить deduplicate_payment и идемпотентность оплаты"}
        batches, chunks, profile = plan(source, snapshot, {"files": [{"path": "billing.py", "status": "modified", "patch": "+def deduplicate_payment(payment_id):"}]}, None, [], [], 8000)
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(len(json.dumps(payload, ensure_ascii=False)) <= 8000 for payload in batches))
        self.assertEqual(chunks, [chunk for payload in batches for chunk in payload["source_excerpts"]])
        for path, content in files.items():
            self.assertEqual("".join(c["content"] for c in chunks if c["kind"] == "file" and c["path"] == path), content.replace("\r\n", "\n"))
        billing = [e for e in chunks if e["kind"] == "file" and e["path"] == "billing.py"]
        self.assertTrue(any("def deduplicate_payment" in e["content"] for e in billing))
        self.assertTrue(any(e.get("category") == "tests" for e in chunks))
        self.assertIn("fastapi", profile["dependencies"])
        single_line = "x" * 10000 + "tail"
        self.assertEqual("".join(c["content"] for c in split_text(single_line, 1800)), single_line)

    def test_citations_are_checked_and_documentation_does_not_prove_code(self):
        payload = {"source_excerpts": [
            {"id": "S1", "kind": "file", "path": "app.py", "start_line": 101, "end_line": 103,
             "content": 'def create_order():\n    return "pending"\n'},
            {"id": "S2", "kind": "file", "path": "README.md", "start_line": 1, "end_line": 1,
             "content": "Платежи работают без ошибок"},
            {"id": "S3", "kind": "description", "path": "описание пользователя", "content": "Нужна авторизация клиентов"},
        ]}
        citations = verify_citations([
            {"source_id": "S1", "quote": 'return "pending"', "start_line": 1},
            {"source_id": "S999", "quote": "fictional evidence"},
            {"source_id": "S1", "quote": "def authenticate_user():"},
        ], payload)
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0]["start_line"], 102)
        report = {"recommendations": [
            {"citations": [{"source_id": "S999", "quote": "Does not exist"}], "confidence": "high"},
            {"citations": [{"source_id": "S1", "quote": 'return "pending"'}], "confidence": "medium"},
        ], "existing_features": [
            {"status": "confirmed", "citations": [{"source_id": "S2", "quote": "Платежи работают без ошибок"}]},
            {"status": "confirmed", "citations": [{"source_id": "S1", "quote": "def create_order():"}]},
        ]}
        ground_report(report, payload)
        self.assertEqual(report["recommendations"][0]["grounding"], "hypothesis")
        self.assertEqual(report["recommendations"][0]["confidence"], "low")
        self.assertEqual([f["status"] for f in report["existing_features"]], ["assumed", "confirmed"])

    def test_zip_reads_all_files_and_filters_cache_and_secrets(self):
        files = {f"module_{i:03d}.py": f"VALUE = {i}\n" for i in range(125)}
        files.update({p: "SECRET_DO_NOT_SEND" for p in (
            ".env", "private_key.txt", "node_modules/x.js", "__pycache__/data.json", ".pytest_cache/data.json",
            ".mypy_cache/example.data.json", ".ruff_cache/data.txt", ".next/build.js", "pkg/example.pyc", "coverage/index.html",
        )})
        before = zip_snapshot(archive(files), "a.zip")
        self.assertEqual(len(before["files"]), 125)
        self.assertEqual(len(before["inventory"]), 125)
        self.assertNotIn("SECRET_DO_NOT_SEND", json.dumps(before))
        self.assertEqual(sum(before["excluded"].values()), 10)
        files["module_124.py"] = "VALUE = 9999\n"
        after = zip_snapshot(archive(files), "b.zip")
        self.assertNotEqual(before["ref"], after["ref"])
        result = compare_snapshots({"kind": "zip"}, before, after)
        self.assertTrue(any(f["path"] == "module_124.py" for f in result["files"]))

    def test_secret_redaction_preserves_line_numbers(self):
        code = 'KEY = """-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"""\ndef useful():\n    return 42\n'
        clean = source_excerpt("app.py", code.encode())["content"]
        self.assertNotIn("\nsecret\n", clean)
        self.assertEqual(clean.splitlines().index("def useful():"), code.splitlines().index("def useful():"))
        self.assertEqual(redact('api_key="SHOULD_NOT_LEAK"\nreturn 42'), '[скрыто]\nreturn 42')

    def test_valid_citation_is_authoritative_even_without_redundant_source_field(self):
        payload = {"source_excerpts": [{"id": "S1", "kind": "file", "path": "app.py", "start_line": 1,
                                        "content": 'def create_order():\n    return "pending"'}]}
        report = validate_analysis({"recommendations": [], "existing_features": [{
            "title": "Начальный статус заказа", "status": "confirmed",
            "citations": [{"source_id": "S1", "quote": 'return "pending"'}],
        }]}, {"hourly_rate": 1000, "minimum_price": 1000, "currency": "₽"}, ["app.py"], payload)
        self.assertEqual(report["existing_features"][0]["status"], "confirmed")
        self.assertEqual(report["existing_features"][0]["source"], "app.py")

    def test_text_falling_outside_sampling_is_not_a_file_deletion(self):
        manifest = [{"path": "a.py", "crc": 11, "size": 12}, {"path": "b.py", "crc": 22, "size": 12}]
        before = {"ref": "a", "inventory": manifest, "files": [{"path": "a.py", "digest": "old", "content": "return 1"}]}
        after = {"ref": "b", "inventory": manifest, "files": [{"path": "b.py", "digest": "other", "content": "return 2"}]}
        result = compare_snapshots({"kind": "zip"}, before, after)
        self.assertEqual(result["files"], [])

    def test_legacy_database_is_migrated_without_losing_feedback(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "legacy.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE users(user_id INTEGER PRIMARY KEY,hourly_rate INTEGER DEFAULT 1500,minimum_price INTEGER DEFAULT 3000,currency TEXT DEFAULT '₽',mode TEXT DEFAULT '',draft TEXT DEFAULT '{}');
                CREATE TABLE training(id INTEGER PRIMARY KEY,user_id INTEGER,source TEXT,summary TEXT,ideas TEXT,created_at TEXT);
                CREATE TABLE feedback(id INTEGER PRIMARY KEY,training_id INTEGER,user_id INTEGER,idea_index INTEGER,verdict TEXT,comment TEXT,created_at TEXT);
                INSERT INTO users(user_id) VALUES(1);
                INSERT INTO training VALUES(1,1,'old.zip','Старый проект','[]','2026-01-01');
                INSERT INTO feedback VALUES(1,1,1,-1,'own','Не потерять эту идею','2026-01-01');
            """)
            conn.close()
            with patch.dict(os.environ, {"OBERON_DB": str(path)}):
                conn = db_connect()
                self.assertEqual(conn.execute("SELECT comment FROM feedback").fetchone()[0], "Не потерять эту идею")
                self.assertEqual(conn.execute("SELECT enabled,scope FROM feedback").fetchone()[1], "similar")
                conn.close()
                db_connect().close()  # Migration is idempotent.

    def test_similar_feedback_excludes_unrelated_and_disabled_examples(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.dict(os.environ, {"OBERON_DB": str(Path(directory) / "memory.sqlite3")}):
            conn = db_connect()
            conn.execute("INSERT INTO users(user_id) VALUES(1)")
            context = {"identity": "old", "terms": ["astronomy", "telescope"], "dependencies": []}
            conn.execute("INSERT INTO training(user_id,source,summary,ideas,context) VALUES(1,'astro.zip','Astronomy telescope','[]',?)", (json.dumps(context),))
            conn.execute("INSERT INTO feedback(training_id,user_id,idea_index,verdict,comment,scope) VALUES(1,1,-1,'own','Calibrate the telescope','similar')")
            conn.commit()
            self.assertEqual(select_feedback(conn, 1, {"identity": "new", "terms": ["restaurant", "payment"]}), [])
            matched = select_feedback(conn, 1, {"identity": "new", "terms": ["astronomy", "telescope"]})
            self.assertEqual(len(matched), 1)
            conn.execute("UPDATE feedback SET enabled=0")
            self.assertEqual(select_feedback(conn, 1, context), [])
            conn.close()


if __name__ == "__main__":
    unittest.main()
