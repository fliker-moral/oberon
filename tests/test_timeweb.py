import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import Mock, patch

from oberon.core import IncompleteResponse
from oberon.pipeline import progress_callback
from oberon.providers import TIMEWEB_SOCKET_TIMEOUT, TextModel


def chat_response(*chunks):
    body = b"".join(b"data: " + json.dumps(chunk).encode() + b"\r\n\r\n" for chunk in chunks)
    stream = io.BytesIO(b": keepalive\r\n\r\n" + body + b"data: [DONE]\r\n\r\n")
    stream.headers = Message()
    stream.headers["Content-Type"] = "text/event-stream; charset=utf-8"
    return stream


class TimewebTransportTests(unittest.TestCase):
    def setUp(self):
        self.model = TextModel("timeweb", "fake-key", "dashscope/qwen3.8-flash", 24000, 5000, False,
                               timeweb_base_url="https://api.timeweb.ai/v1")

    def test_stream_dialog_reads_chunks_reports_progress_and_returns_json(self):
        response = chat_response(
            {"choices": [{"delta": {"role": "assistant", "content": '{"summary":'}}]},
            {"choices": [{"delta": {"content": '"готово"}'}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"total_tokens": 120}},
        )
        phases = []
        callback = progress_callback.set(lambda values: phases.append(values.get("phase")))
        try:
            with patch("oberon.providers.urllib.request.build_opener") as build:
                open_request = build.return_value.open
                open_request.return_value = response
                self.assertEqual(self.model.complete_json("system", {"task": "source_review"}), {"summary": "готово"})
                wire = open_request.call_args.args[0]
                self.assertTrue(json.loads(wire.data)["stream"])
                self.assertIn("text/event-stream", wire.headers["Accept"])
                self.assertEqual(open_request.call_args.kwargs["timeout"], TIMEWEB_SOCKET_TIMEOUT)
                self.assertEqual(open_request.call_count, 1)
        finally:
            progress_callback.reset(callback)
        self.assertEqual([phase for phase in phases if phase], ["waiting", "receiving"])

    def test_timeout_identifies_the_network_phase_and_does_not_repeat_the_request(self):
        with patch("oberon.providers.urllib.request.build_opener") as build:
            open_request = build.return_value.open
            open_request.side_effect = urllib.error.URLError(TimeoutError())
            with self.assertRaisesRegex(ValueError, "HTTP-заголовки"):
                self.model.complete_json("", {})
            self.assertEqual(open_request.call_count, 1)
        with patch("oberon.providers.urllib.request.build_opener") as build:
            open_request = build.return_value.open
            response = chat_response()
            response.readline = Mock(side_effect=TimeoutError())
            open_request.return_value = response
            with self.assertRaisesRegex(ValueError, "ответ начался"):
                self.model.complete_json("", {})
            self.assertEqual(open_request.call_count, 1)

    def test_finish_reason_does_not_wait_for_the_stream_to_close(self):
        class StalledAfterStop(io.BytesIO):
            def readline(self, size=-1):
                line = super().readline(size)
                if not line:
                    raise TimeoutError()
                return line

        chunk = {"choices": [{"delta": {"content": '{"summary":"ok"}'}, "finish_reason": "stop"}]}
        response = StalledAfterStop(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        response.headers = Message()
        response.headers["Content-Type"] = "text/event-stream"
        with patch("oberon.providers.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            self.assertEqual(self.model.complete_json("system", {}), {"summary": "ok"})

    def test_incomplete_stream_is_not_retried_or_accepted_as_a_report(self):
        with patch("oberon.providers.urllib.request.build_opener") as build:
            open_request = build.return_value.open
            open_request.return_value = chat_response({"choices": [{"delta": {"content": "{}"}}]})
            with self.assertRaisesRegex(IncompleteResponse, "до завершения"):
                self.model.complete_json("", {})
            self.assertEqual(open_request.call_count, 1)
