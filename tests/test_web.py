"""Exercise the desktop service with an isolated database, Git tree and stubbed model."""

import base64
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from oberon.core import IncompleteResponse, db_connect
from oberon.local_git import git_executable
from oberon.providers import PROVIDERS
from oberon.providers import ProviderTransportError
from oberon.web import AppServer
from oberon.workspace import prepare


class ExampleModel:
    model = "deepseek-flash"
    max_input_chars = 8000

    def complete_json(self, system, payload):
        serialized = json.dumps(payload, ensure_ascii=False)
        assert len(serialized) <= self.max_input_chars
        assert "SHOULD_NOT_LEAK" not in serialized
        if payload.get("task") == "training":
            return {"summary": "Учебный проект", "ideas": [{
                "title": "Ввести статусы", "description": "Показывать этап заявки клиенту и менеджеру.",
                "reason": "Удобнее проверять заявки", "business_value": "Меньше потерянных заявок",
                "success_metric": "Доля заявок, обработанных в срок за неделю",
            }]}
        return {
            "project_name": "Учебный проект", "summary": "Учёт заявок",
            "existing_features": [{"title": "Ввод заявки", "status": "confirmed", "source": "лже-описание пользователя"}],
            "recommendations": [{
                "title": "Статусы", "description": "Добавить статусы заявок", "tasks": ["Таблица статусов"],
                "hours_min": 3, "hours_max": 6, "business_value": "Меньше потерянных заявок",
                "success_metric": "Доля заявок, обработанных в срок за неделю",
                "client_pitch": "Предлагаю добавить статусы", "priority": {"malformed": True},
            }],
        }


def encoded_zip(content, date=(2025, 1, 1, 0, 0, 0)):
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w") as archive:
        archive.writestr(zipfile.ZipInfo("project/app.py", date), content)
        archive.writestr("project/.env", "DEEPSEEK_API_KEY=SHOULD_NOT_LEAK")
        archive.writestr("project/credentials.json", '{"value":"SHOULD_NOT_LEAK"}')
    return base64.b64encode(memory.getvalue()).decode()


class DesktopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.db = self.directory / "test.sqlite3"
        variables = patch.dict(os.environ, {"OBERON_DB": str(self.db), "DEEPSEEK_API_KEY": "", "GITHUB_TOKEN": ""})
        variables.start()
        self.addCleanup(variables.stop)
        with closing(db_connect()) as conn:
            prepare(conn)
        self.server = AppServer(desktop_token="local-window-proof-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.token = self.request("/api/bootstrap")["request_token"]

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, data=None, *, desktop=True, raw=False):
        headers = {"X-Oberon-Desktop": "local-window-proof-token"} if desktop else {}
        body = None
        if data is not None:
            headers.update({"Content-Type": "application/json", "X-Oberon-Token": self.token})
            body = json.dumps(data).encode()
        with urllib.request.urlopen(urllib.request.Request(self.base + path, body, headers), timeout=8) as response:
            return response.read() if raw else json.load(response)

    def action(self, endpoint, **data):
        return self.request("/api/" + endpoint, data)

    def test_offline_project_task_offer_and_backup(self):
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.request("/api/bootstrap", desktop=False)
        self.assertEqual(rejected.exception.code, 403)
        project = self.action("manual", name="Личное приложение", description="Доработать форму заявок")
        project_id = project["id"]
        project = self.action("task", project_id=project_id, title="Уточнить требования")
        item = project["items"][0]
        updated = self.action("task", project_id=project_id, **{**item, "status": "done"})
        self.assertEqual(updated["items"][0]["status"], "done")
        self.action("save-offer", project_id=project_id, text="Черновик предложения")
        exported = self.request(f"/api/export?id={project_id}&kind=offer", raw=True).decode("utf-8-sig")
        self.assertEqual(exported, "Черновик предложения")
        self.action("settings", deepseek_key="fake-key-for-test")
        with closing(sqlite3.connect(self.db)) as conn:
            value = conn.execute("SELECT value FROM app_secrets").fetchone()[0]
            self.assertTrue(value.startswith("dpapi:"))
            self.assertNotIn("fake-key-for-test", value)
        with closing(sqlite3.connect(":memory:")) as backup:
            backup.deserialize(self.request("/api/backup", raw=True))
            self.assertEqual(backup.execute("SELECT count(*) FROM app_secrets").fetchone()[0], 0)
            self.assertEqual(backup.execute("SELECT status FROM work_items").fetchone()[0], "done")

    def test_commercial_ideas_feed_project_and_training_offers(self):
        model = ExampleModel()
        technical = {"title": "Рефакторинг", "description": "Переименовать методы."}
        opportunity = {
            "title": "Конверсия по товару",
            "description": "Учитывать user_id и order_id; владелец увидит просмотры и оплаченные заказы по каждому товару.",
            "business_value": "Владелец увидит, какие товары привлекают внимание, но редко покупаются.",
            "success_metric": "Уникальные покупатели товара / уникальные посетители его карточки за неделю.",
            "hours_min": None, "hours_max": None,
        }

        def respond(prompt, payload):
            self.assertIn("Рефакторинг", prompt)
            self.assertIn("конверсии", prompt)
            self.assertIn("client_title и client_pitch", prompt)
            self.assertTrue(any("заказы из Telegram" in excerpt["content"] for excerpt in payload["source_excerpts"]))
            key = "ideas" if payload["task"] == "training" else "recommendations"
            return {"project_name": "Магазин", key: [technical, opportunity]}

        model.complete_json = respond
        form = dict(kind="zip", name="store.zip", content=encoded_zip("def list_products(): pass"),
                    description="Увеличить оплаченные заказы из Telegram")
        with patch("oberon.web.llm", return_value=model):
            project = self.action("analyze", **form)
            review = self.action("train", **form)
        recommendations = project["report"]["recommendations"]
        self.assertEqual([entry["title"] for entry in recommendations], [opportunity["title"]])
        self.assertIsNone(recommendations[0]["cost_min"])
        self.assertEqual(len(review["ideas"]), 1)
        for route, identity in (("offer", {"project_id": project["id"]}), ("training-offer", {"training_id": review["id"]})):
            offer = self.action(route, indices=[0], **identity)["text"]
            self.assertIn("проекта «Магазин»", offer)
            self.assertIn(opportunity["business_value"], offer)
            self.assertIn("Владелец увидит просмотры и оплаченные заказы по каждому товару.", offer)
            self.assertNotIn(opportunity["success_metric"], offer)
            self.assertNotIn("user_id", offer)
            self.assertNotIn("order_id", offer)
            self.assertIn("Стоимость уточним", offer)
            self.assertNotIn(technical["title"], offer)
        self.action("price", project_id=project["id"], index=0, version_ref=project["report"]["version_ref"],
                    cost_min=9000, cost_max=12000)
        priced = self.action("offer", project_id=project["id"], indices=[0])["text"]
        self.assertIn("9 000–12 000", priced)

        model.complete_json = lambda *_: {"project_name": "Магазин", "ideas": [{
            **opportunity, "client_title": "Отчёт о продажах по товарам",
            "client_pitch": "Для отчёта используем user_id. "
                            "Вы увидите, какие товары чаще смотрят и какие чаще покупают.",
        }]}
        with patch("oberon.web.llm", return_value=model):
            review = self.action("train", **form)
        offer = self.action("training-offer", training_id=review["id"], indices=[0])["text"]
        self.assertIn("Отчёт о продажах по товарам", offer)
        self.assertIn("Вы увидите, какие товары чаще смотрят и какие чаще покупают.", offer)
        self.assertNotIn("user_id", offer)
        self.assertNotIn("Конверсия по товару", offer)

    def test_timeweb_saves_partial_training_and_splits_only_the_failed_batch_on_resume(self):
        model = ExampleModel()
        model.provider = "timeweb"
        sent = []
        first_attempt = True
        fields = {"title": "Отчёт по оплатам", "business_value": "Быстрее сверять оплаченные заказы.",
                  "success_metric": "Минуты на еженедельную сверку."}

        def respond(prompt, payload):
            if payload["task"] == "training":
                return {"summary": "Сводка", "ideas": [{**fields, "description": "Добавить отчёт об оплатах."}]}
            source_ids = tuple(e["id"] for e in payload["source_excerpts"])
            sent.append(source_ids)
            if first_attempt and len(sent) == 2:
                raise ProviderTransportError("Таймаут чтения", "body", TimeoutError())
            return {"summary": "Пакет проверен", "findings": [
                {**fields, "kind": "opportunity", "proposal": "Добавить отчёт об оплатах."},
            ]}

        def run(job_id):
            self.action("train", job_id=job_id, background=True, kind="zip", name="store.zip",
                        content=encoded_zip("# store\n" * 8500))
            for _ in range(200):
                status = self.request("/api/progress?id=" + job_id)
                if status["state"] != "running":
                    self.assertEqual(status["state"], "success", status)
                    return status, self.request("/api/training?id=" + str(status["result_id"]))
                time.sleep(0.01)
            self.fail("Background training did not finish.")

        model.complete_json = respond
        with patch("oberon.web.llm", return_value=model):
            status, partial = run("paused-with-result")
            self.assertIn("частичный", status["message"])
            audit = partial["metadata"]
            self.assertEqual((audit["coverage_mode"], audit["completed_batches"]), ("partial", 1))
            self.assertEqual(len(sent), 2)
            self.assertEqual(tuple(e["id"] for e in audit["source_excerpts"]), sent[0])
            self.assertIn("Отчёт по оплатам", self.action("training-offer", training_id=partial["id"], indices=[0])["text"])
            first, failed = sent[:2]
            first_attempt = False
            status, full = run("resumed-after-timeout")
        self.assertEqual(full["metadata"]["coverage_mode"], "complete")
        self.assertEqual(full["metadata"]["reused_requests"], 1)
        self.assertNotIn(first, sent[2:])
        self.assertEqual(sent[2] + sent[3], failed)
        with closing(db_connect()) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM analysis_batch_splits").fetchone()[0], 0)

    def test_analysis_and_learning_after_invalid_archive(self):
        with patch("oberon.web.llm", return_value=ExampleModel()):
            with self.assertRaises(urllib.error.HTTPError) as invalid:
                self.action("analyze", kind="zip", name="bad.zip", content="dGVzdA==")
            self.assertEqual(invalid.exception.code, 400)
            self.assertIn("ZIP", json.load(invalid.exception)["error"])
            archive = encoded_zip('password="SHOULD_NOT_LEAK"\nprint("ok")')
            project = self.action("analyze", kind="zip", name="project.zip", content=archive)
            self.assertEqual(project["report"]["recommendations"][0]["cost_min"], 4500)
            self.assertEqual(project["report"]["existing_features"][0]["status"], "assumed")
            offer = self.action("offer", project_id=project["id"], indices=[0])
            self.assertIn("4 500", offer["text"])
            review = self.action("train", kind="zip", name="training.zip", content=archive)
            self.action("feedback", training_id=review["id"], idea_index=0, verdict="good", comment="Подходит")
            review = self.action("feedback", training_id=review["id"], idea_index=0, verdict="bad", comment="Требует уточнения")
            self.assertEqual([item["verdict"] for item in review["feedback"]], ["bad"])

    def test_learning_memory_scope_edit_disable_and_refine(self):
        model = ExampleModel()
        captured = []
        original = model.complete_json

        def capture(system, payload):
            captured.append(payload)
            return original(system, payload)

        model.complete_json = capture
        with patch("oberon.web.llm", return_value=model):
            archive = encoded_zip("def submit_order():\n    return 'pending'\n")
            review = self.action("train", kind="zip", name="learning.zip", content=archive,
                                 description="Заказы ресторана, статусы доставки")
            self.assertIn("source_excerpts", review["metadata"])
            review = self.action("feedback", training_id=review["id"], idea_index=0, rating=3,
                                 scope="project", comment="Статусы уже есть, проверять переходы",
                                 correction="Тестировать переход в статус «В доставке»")
            feedback_id = review["feedback"][0]["id"]
            self.assertEqual(review["feedback"][0]["rating"], 3)
            refined = self.action("retrain", training_id=review["id"])
            self.assertNotEqual(refined["id"], review["id"])
            self.assertEqual(refined["metadata"]["parent_training_id"], review["id"])
            self.assertEqual(captured[-1]["feedback_examples"][0]["id"], feedback_id)
            self.assertEqual(captured[-1]["feedback_examples"][0]["correction"], "Тестировать переход в статус «В доставке»")
            self.action("analyze", kind="zip", name="other.zip", content=archive, description="Статусы доставки ресторана")
            self.assertEqual(captured[-1]["feedback_examples"], [])
            review = self.action("memory", training_id=review["id"], id=feedback_id, operation="edit",
                                 scope="global", comment="Перед оценкой уточнять существующие функции")
            self.action("analyze", kind="auto", text="Проект интернет магазина")
            self.assertEqual(captured[-1]["feedback_examples"][0]["scope"], "global")
            self.action("memory", training_id=review["id"], id=feedback_id, operation="toggle")
            self.action("retrain", training_id=review["id"])
            self.assertEqual(captured[-1]["feedback_examples"], [])
            self.action("memory", training_id=review["id"], id=feedback_id, operation="toggle")
            self.action("settings", use_feedback=False)
            self.action("retrain", training_id=review["id"])
            self.assertEqual(captured[-1]["feedback_examples"], [])
            self.assertEqual(len(self.request("/api/training?id=" + str(review["id"]))["feedback"]), 1)
            review = self.action("memory", training_id=review["id"], id=feedback_id, operation="delete")
            self.assertEqual(review["feedback"], [])

    def test_full_analysis_of_local_archive_uses_tail_and_multiple_batches(self):
        model = ExampleModel()
        captured = []
        last_line = "def final_source_marker():"

        def respond(system, payload):
            captured.append(payload)
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False)), model.max_input_chars)
            if payload["task"] == "source_review":
                for excerpt in payload["source_excerpts"]:
                    if last_line in excerpt["content"]:
                        return {"summary": "Обработан последний фрагмент", "findings": [{
                            "title": "Обработчик", "observation": "В конце файла", "kind": "feature",
                            "citations": [{"source_id": excerpt["id"], "quote": last_line}],
                        }]}
                return {"summary": "Обработан пакет исходников. " * 5, "findings": []}
            if payload["task"] == "reduce":
                findings = [note for note in payload["notes"] if note.get("citations")]
                return {"summary": "Итоги пакетов", "findings": findings[:1]}
            citation = next(c for note in payload["notes"] for c in note.get("citations", []) if c["quote"] == last_line)
            return {"project_name": "Пакетный отчёт", "recommendations": [],
                    "existing_features": [{"title": "Последний обработчик", "status": "confirmed", "citations": [citation]}]}

        model.complete_json = respond
        archive_path = self.directory / "big.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("repo/app.py", "# comment in a long file\n" * 25000 + last_line + "\n    return 42\n")
            archive.writestr("repo/__pycache__/fake.json", "SHOULD_NOT_LEAK")
        with patch("oberon.web.llm", return_value=model):
            result = self.action("analyze", kind="zip", archive_path=str(archive_path), job_id="large-run")
        audit = result["report"]["audit"]
        self.assertGreater(audit["completed_batches"], 1)
        self.assertEqual(audit["completed_batches"], audit["total_batches"])
        self.assertTrue(any(p["task"] == "reduce" for p in captured))
        self.assertEqual(audit["excluded"], {"cache_or_dependencies": 1})
        feature = result["report"]["existing_features"][0]
        self.assertEqual(feature["status"], "confirmed")
        self.assertEqual(feature["citations"][0]["start_line"], 25001)
        self.assertEqual(self.request("/api/progress?id=large-run")["stage"], "save")
        self.assertNotIn("SHOULD_NOT_LEAK", json.dumps(captured))

    def test_cancelled_batch_does_not_save_a_partial_report(self):
        model = ExampleModel()
        entered, release = threading.Event(), threading.Event()

        def first_batch(system, payload):
            entered.set()
            if not release.wait(4):
                raise AssertionError("Test did not release the model")
            return {"summary": "Пакет прочитан", "findings": []}

        model.complete_json = first_batch
        errors = []

        def analyze():
            try:
                self.action("analyze", kind="zip", name="large.zip", content=encoded_zip("print('x')\n" * 2000), job_id="cancel-me")
            except urllib.error.HTTPError as error:
                errors.append(json.load(error)["error"])

        with patch("oberon.web.llm", return_value=model):
            worker = threading.Thread(target=analyze)
            worker.start()
            try:
                self.assertTrue(entered.wait(4))
                self.assertEqual(self.request("/api/progress?id=cancel-me")["stage"], "map")
                self.assertTrue(self.action("cancel", job_id="cancel-me")["cancel_requested"])
            finally:
                release.set()
                worker.join(6)
        self.assertEqual(errors, ["Анализ отменён"])
        self.assertEqual(self.request("/api/bootstrap")["projects"], [])

    def test_background_analysis_preserves_failed_state_and_reuses_successful_batches(self):
        entered, release = threading.Event(), threading.Event()
        model = ExampleModel()
        tasks = []
        fail_final = True

        def respond(system, payload):
            task = payload["task"]
            tasks.append(task)
            if len(tasks) == 1:
                entered.set()
                if not release.wait(4):
                    raise AssertionError("Test did not release the worker")
            if task == "source_review":
                return {"summary": "Пакет прочитан", "findings": []}
            if fail_final:
                raise IncompleteResponse("Модель не закончила ответ")
            return ExampleModel().complete_json(system, payload)

        def wait_for_completion(job_id, expected):
            for _ in range(200):
                status = self.request("/api/progress?id=" + job_id)
                if status["state"] != "running":
                    self.assertEqual(status["state"], expected, status)
                    return status
                time.sleep(0.01)
            self.fail("Background analysis did not finish")

        model.complete_json = respond
        request_data = dict(background=True, kind="zip", name="resume.zip", content=encoded_zip("# example\n" * 4000))
        with patch("oberon.web.llm", return_value=model):
            try:
                self.assertEqual(self.action("analyze", job_id="first", **request_data), {"id": "first"})
                self.assertTrue(entered.wait(4))
                self.assertEqual(self.request("/api/bootstrap")["pending_job"], "first")
                self.assertEqual(self.request("/api/progress?id=first")["state"], "running")
                self.assertEqual(self.action("analyze", job_id="first", **request_data), {"id": "first"})
            finally:
                release.set()
            failed = wait_for_completion("first", "failed")
            self.assertEqual(failed["stage"], "final")
            self.assertIn("Модель не закончила ответ", failed["message"])
            self.assertFalse(self.request("/api/bootstrap")["projects"])
            with self.server.jobs_lock:
                self.server.jobs.clear()
            self.assertEqual(self.request("/api/progress?id=first")["message"], failed["message"])

            completed_batches = tasks.count("source_review")
            self.assertGreater(completed_batches, 1)
            fail_final = False
            self.action("analyze", job_id="second", **request_data)
            success = wait_for_completion("second", "success")

        audit = self.request("/api/project?id=" + str(success["result_id"]))["report"]["audit"]
        self.assertEqual(tasks.count("source_review"), completed_batches)
        self.assertEqual(audit["reused_requests"], completed_batches)
        self.assertEqual(audit["requests"], 1)
        with closing(db_connect()) as conn:
            row = conn.execute("SELECT result_id FROM analysis_runs WHERE id='second'").fetchone()
            self.assertEqual(row["result_id"], success["result_id"])
            self.assertEqual(conn.execute("SELECT count(*) FROM analysis_responses").fetchone()[0], 0)
        self.action("dismiss-job", job_id="second")
        self.assertIsNone(self.request("/api/bootstrap")["pending_job"])

    def test_zip_repack_is_not_a_new_snapshot(self):
        data = encoded_zip("print('first')")
        project = self.action("analyze", kind="zip", name="one.zip", content=data, snapshot_only=True)
        with self.assertRaises(urllib.error.HTTPError) as unchanged:
            self.action(
                "refresh", project_id=project["id"], name="two.zip",
                content=encoded_zip("print('first')", (2026, 1, 1, 0, 0, 0)), snapshot_only=True,
            )
        self.assertIn("не изменился", json.load(unchanged.exception)["error"])
        updated = self.action(
            "refresh", project_id=project["id"], name="two.zip",
            content=encoded_zip("print('second')"), snapshot_only=True,
        )
        self.assertEqual(updated["changes"]["files"][0]["status"], "modified")

    def test_provider_switch_remembers_models_and_routes_requests(self):
        report = ExampleModel().complete_json("", {})
        replies = {
            "deepseek": {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(report)}}]},
            "gemini": {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(report)}]}}]},
            "openai": {"status": "completed", "output": [
                {"type": "reasoning"},
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(report)}]},
            ]},
        }
        configs = {
            "deepseek": ("deepseek-v4-pro", "/chat/completions", "deepseek_key"),
            "gemini": ("gemini-3.8-flash", "/models/gemini-3.8-flash:generateContent", "gemini_key"),
            "openai": ("gpt-5.3-codex", "/responses", "openai_key"),
        }
        for provider, (model, endpoint, key_field) in configs.items():
            with self.subTest(provider=provider):
                key = f"synthetic-key-for-{provider}"
                settings = self.action("settings", provider=provider, model=model, **{key_field: key})
                self.assertEqual(settings[provider], "stored")
                self.assertNotIn(key, json.dumps(settings))
                with patch("oberon.providers.api_request", return_value=replies[provider]) as wire:
                    project = self.action("analyze", kind="auto", text="Программа для обработки заявок")
                self.assertEqual(project["report"]["model"], model)
                self.assertEqual(project["report"]["provider"], provider)
                spec, credential, path, body = wire.call_args.args
                self.assertIs(spec, PROVIDERS[provider])
                self.assertEqual(credential, key)
                self.assertEqual(path, endpoint)
                if provider == "openai":
                    self.assertFalse(body["store"])
                    self.assertEqual(body["text"]["format"]["type"], "json_object")
                elif provider == "gemini":
                    self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        settings = self.action("settings", provider="deepseek")
        self.assertEqual(settings["model"], "deepseek-v4-pro")
        self.assertEqual(settings["provider_models"]["openai"], "gpt-5.3-codex")
        with self.assertRaises(urllib.error.HTTPError):
            self.action("settings", provider="gemini", model="gpt-5.3-codex")
        self.assertEqual(self.request("/api/bootstrap")["settings"]["provider"], "deepseek")
        listed = {"models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.1-flash-image", "supportedGenerationMethods": ["generateContent"]},
        ]}
        with patch("oberon.providers.api_request", return_value=listed):
            self.assertEqual([model["id"] for model in self.action("check", service="gemini")["models"]],
                             ["gemini-3.8-flash"])

    def test_qwen_uses_workspace_host_and_json_for_training_and_connection_check(self):
        host = "llm-test.eu-central-1.maas.aliyuncs.com"
        key = "fake-qwen-key-for-test"
        settings = self.action("settings", provider="qwen", model="qwen3.7-plus",
                               qwen_host=f"https://{host}/compatible-mode/v1/", qwen_key=key, thinking=True)
        self.assertEqual(settings["qwen_host"], host)
        self.assertEqual(settings["qwen"], "stored")
        self.assertNotIn(key, json.dumps(settings))
        with self.assertRaises(urllib.error.HTTPError):
            self.action("settings", qwen_host=host + ".example.com")
        self.assertEqual(self.request("/api/bootstrap")["settings"]["qwen_host"], host)

        def reply(spec, credential, path, body):
            self.assertEqual(spec.base_url, f"https://{host}/compatible-mode/v1")
            self.assertEqual(spec.headers(credential), {"Authorization": f"Bearer {key}"})
            self.assertEqual(path, "/chat/completions")
            self.assertEqual(body["model"], "qwen3.7-plus")
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertNotIn("thinking", body)
            if body["enable_thinking"]:
                result = {"summary": "Qwen test", "ideas": [{
                    "title": "Упростить вывод", "description": "Вывести статусы заявок в кабинете клиента.",
                    "reason": "Проверка", "business_value": "Сократить обращения о статусе",
                    "success_metric": "Число обращений о статусе за неделю",
                }]}
            else:
                self.assertEqual(body["max_tokens"], 64)
                result = {"ok": True}
            return {"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(result), "reasoning_content": "not the JSON result",
            }}]}

        with patch("oberon.providers.api_request", side_effect=reply) as wire:
            checked = self.action("check", service="qwen")
            trained = self.action("train", kind="zip", name="project.zip", content=encoded_zip("print('example')"))
        self.assertIn("Qwen3.7 Plus", checked["message"])
        self.assertEqual(trained["ideas"][0]["title"], "Упростить вывод")
        self.assertEqual(trained["metadata"]["provider"], "qwen")
        self.assertEqual([call.args[3]["enable_thinking"] for call in wire.call_args_list], [False, True])

    @unittest.skipUnless(git_executable(), "Git для Windows необходим для локального сценария")
    def test_real_local_git_refresh_and_diff(self):
        repository = self.directory / "repo"
        repository.mkdir()

        git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        git_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

        def git(*args):
            return subprocess.check_output(
                [git_executable(), "-C", str(repository), "-c", "user.name=QA", "-c", "user.email=qa@example.test", *args],
                env=git_env,
            ).decode().strip()

        git("init")
        code = repository / "app.py"
        code.write_text("print('first')", encoding="utf-8")
        git("add", "app.py")
        git("commit", "-m", "First")
        base = git("rev-parse", "HEAD")
        project = self.action("analyze", kind="local_git", path=str(repository), snapshot_only=True)
        self.assertEqual(project["report"]["version_ref"], base)
        code.write_text("print('second')", encoding="utf-8")
        git("add", "app.py")
        git("commit", "-m", "Second")
        head = git("rev-parse", "HEAD")
        diff = self.action("git-diff", project_id=project["id"], base=base, head=head)
        self.assertEqual(diff["commits"][0]["message"], "Second")
        self.assertIn("+print('second')", diff["files"][0]["patch"])
        updated = self.action("refresh", project_id=project["id"], snapshot_only=True)
        self.assertEqual(len(updated["versions"]), 2)
        result = self.action(
            "compare", project_id=project["id"], old_id=updated["versions"][1]["id"],
            new_id=updated["versions"][0]["id"],
        )
        self.assertEqual(result["files"][0]["path"], "app.py")


if __name__ == "__main__":
    unittest.main()
