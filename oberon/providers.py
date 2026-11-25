"""Text generation through provider APIs without running a coding agent."""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace

from .core import IncompleteResponse
from .diagnostics import current_run, logger
from .pipeline import progress, provider_progress


MAX_RESPONSE_BYTES = 1_500_000
MAX_STREAM_BYTES = 24_000_000
TIMEWEB_SOCKET_TIMEOUT = 180
TIMEWEB_STREAM_DEADLINE = 300


@dataclass(frozen=True)
class Provider:
    label: str
    base_url: str
    key_field: str
    models: tuple[tuple[str, str], ...]
    model_pattern: str
    list_path: str = "/models"

    def allows(self, model: object) -> bool:
        return isinstance(model, str) and len(model) <= 100 and re.fullmatch(self.model_pattern, model) is not None

    def headers(self, key: str) -> dict:
        if self.key_field == "gemini_key":
            return {"x-goog-api-key": key}
        return {"Authorization": f"Bearer {key}"}


PROVIDERS = {
    "deepseek": Provider(
        "DeepSeek", "https://api.deepseek.com", "deepseek_key",
        (("deepseek-flash", "DeepSeek Flash"), ("deepseek-v4-pro", "DeepSeek V4 Pro")),
        r"deepseek-[a-zA-Z0-9._-]+",
    ),
    "gemini": Provider(
        "Gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini_key",
        (("gemini-3.8-flash", "Gemini 3.8 Flash"), ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite"),
         ("gemini-3.1-pro-preview", "Gemini 3.1 Pro (Preview)")),
        r"gemini-[0-9.]+-(?:flash(?:-lite)?|pro)(?:-(?:preview(?:-[0-9-]+)?|[0-9]{3}))?",
        "/models?pageSize=100",
    ),
    "openai": Provider(
        "OpenAI API", "https://api.openai.com/v1", "openai_key",
        (("gpt-6.1-sol", "GPT-6.1 Sol"), ("gpt-6.1-terra", "GPT-6.1 Terra"),
         ("gpt-6-luna", "GPT-6 Luna"), ("gpt-5.3-codex", "GPT-5.3-Codex")),
        r"gpt-(?:6(?:\.\d+)?-(?:sol|terra|astra|luna)|5(?:\.\d+)?-codex(?:-max)?)(?:-\d{4}-\d{2}-\d{2})?",
    ),
    "qwen": Provider(
        "Qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "qwen_key",
        (("qwen3.8-flash", "Qwen3.8 Flash"), ("qwen3.7-plus", "Qwen3.7 Plus"), ("qwen4-max", "Qwen4 Max")),
        r"qwen(?:3\.8-flash|3\.7-plus|4-max)",
    ),
    "timeweb": Provider(
        "Qwen через Timeweb AI", "", "timeweb_key",
        (("dashscope/qwen3.8-flash", "Qwen 3.8 Flash"), ("dashscope/qwen3.7-plus", "Qwen 3.7 Plus"),
         ("dashscope/qwen4-max", "Qwen 4 Max")),
        r"dashscope/qwen[0-9.\-]*(?:flash|plus|max)(?:-[a-zA-Z0-9.-]+)?",
    ),
}


def timeweb_provider(base_url: str) -> Provider:
    url = base_url.strip().lower().rstrip("/")
    if not re.fullmatch(r"https://api\.timeweb\.ai/v1", url):
        raise ValueError("Введите базовый URL Timeweb AI Gateway в настройках.")
    return replace(PROVIDERS["timeweb"], base_url=url)


def qwen_api_host(value: str) -> str:
    """Accept the console's API Host or its OpenAI-compatible HTTPS base URL."""
    host = value.strip().lower().removeprefix("https://").rstrip("/").removesuffix("/compatible-mode/v1")
    if not host or host in {"dashscope-intl.aliyuncs.com", "dashscope.aliyuncs.com", "cn-hongkong.dashscope.aliyuncs.com"}:
        return host
    if re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\."
        r"(?:ap-southeast-1|us-east-1|cn-beijing|cn-hongkong|ap-northeast-1|eu-central-1)\.maas\.aliyuncs\.com",
        host,
    ):
        return host
    raise ValueError("Укажите API Host из Alibaba Cloud Model Studio для вашего ключа Qwen.")


def provider_catalog() -> dict:
    return {
        name: {"label": provider.label, "models": [{"id": model, "name": label} for model, label in provider.models]}
        for name, provider in PROVIDERS.items()
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ProviderTransportError(ValueError):
    def __init__(self, message: str, phase: str, reason: Exception):
        super().__init__(message)
        self.phase = phase
        self.reason = type(reason).__name__


def timeweb_events(response, deadline: float):
    total_bytes = 0
    lines = []
    while True:
        progress("checkpoint")
        if time.monotonic() >= deadline:
            raise IncompleteResponse(f"Timeweb AI · ожидание потокового ответа превысило {TIMEWEB_STREAM_DEADLINE} с.")
        line = response.readline(MAX_RESPONSE_BYTES + 1)
        total_bytes += len(line)
        if len(line) > MAX_RESPONSE_BYTES or total_bytes > MAX_STREAM_BYTES:
            raise IncompleteResponse("Потоковый ответ Timeweb AI превышает допустимый размер.")
        if line.startswith(b"data:"):
            lines.append(line[5:].lstrip(b" ").rstrip(b"\r\n"))
        elif not line.strip() and lines:
            yield b"\n".join(lines)
            lines.clear()
        if not line:
            return


def read_timeweb_stream(response, deadline: float) -> dict:
    fragments, usage, finish = [], None, None
    content_bytes = 0
    for event in timeweb_events(response, deadline):
        if event == b"[DONE]":
            break
        try:
            chunk = json.loads(event)
            if chunk.get("error"):
                raise IncompleteResponse("Timeweb AI · шлюз вернул ошибку во время генерации.")
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if choices:
                choice = choices[0]
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str):
                    content_bytes += len(content.encode())
                    if content_bytes > MAX_RESPONSE_BYTES:
                        raise IncompleteResponse("Текст ответа Timeweb AI превышает допустимый размер.")
                    fragments.append(content)
                finish = choice.get("finish_reason") or finish
                if finish:
                    # The choice is terminal. Do not block on an optional trailing event or EOF.
                    break
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise IncompleteResponse("Timeweb AI · нарушен формат потокового ответа.") from exc
    if not finish:
        raise IncompleteResponse("Timeweb AI · поток закрылся до завершения ответа.")
    if finish not in ("stop", "length"):
        raise IncompleteResponse("Timeweb AI · модель прервала генерацию.")
    return {"choices": [{"message": {"content": "".join(fragments)}, "finish_reason": finish}], "usage": usage}


def transport_error(label: str, phase: str, exc: Exception, timeout: int) -> ProviderTransportError:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, TimeoutError):
        detail = (f"за {timeout} с не получены HTTP-заголовки."
                  if phase == "headers" else f"ответ начался, но при чтении истекло {timeout} с ожидания.")
    elif isinstance(reason, socket.gaierror):
        detail = "DNS не смог определить адрес шлюза."
    elif isinstance(reason, ssl.SSLError):
        detail = "не удалось установить защищённое HTTPS-соединение."
    elif isinstance(reason, (ConnectionError, http.client.IncompleteRead)):
        detail = "соединение оборвалось до получения полного ответа."
    else:
        detail = "не удалось обменяться данными с API."
    return ProviderTransportError(f"{label} · {detail}", phase, reason)


def api_request(provider: Provider, key: str, path: str, body: dict | None = None) -> dict:
    stream = provider.key_field == "timeweb_key" and body is not None and body.get("stream") is True
    accept = "text/event-stream, application/json" if stream else "application/json"
    headers = {"Accept": accept, **provider.headers(key)}
    encoded = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(provider.base_url + path, data=encoded, headers=headers)
    label = provider.label
    phase = "headers"
    timeout = 100 if body is not None else 15
    if stream or provider.key_field == "qwen_key" and body and body.get("enable_thinking"):
        timeout = TIMEWEB_SOCKET_TIMEOUT
    try:
        if stream:
            provider_progress("waiting")
        start = time.monotonic()
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            phase = "body"
            if stream:
                provider_progress("receiving")
                run = current_run.get()
                logger.info("provider_headers id=%s provider=timeweb elapsed_ms=%s",
                            run["id"] if run is not None else "-", round((time.monotonic() - start) * 1000))
                if response.headers.get_content_type() == "text/event-stream":
                    return read_timeweb_stream(response, start + TIMEWEB_STREAM_DEADLINE)
            content = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        details = {
            400: "Запрос отклонён. Проверьте ключ, модель и параметры ответа.",
            401: "Ключ API не принят.", 402: "Недостаточно средств на счёте API.",
            403: "Проверьте разрешения ключа и доступность API.",
            404: "Выбранная модель недоступна. Обновите список моделей в настройках.",
            408: "Шлюз сообщил о таймауте ответа модели (HTTP 408).",
            422: "Шлюз отклонил запрос (HTTP 422). Для Timeweb проверьте лимит расхода ключа.",
            429: "Достигнут лимит запросов или исчерпана квота API.",
            504: "Шлюз сообщил о таймауте ответа модели (HTTP 504).",
        }
        raise ValueError(f"{label} · {details.get(code, f'Ошибка HTTP {code}.')}") from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise transport_error(label, phase, exc, timeout) from exc
    if len(content) > MAX_RESPONSE_BYTES:
        raise IncompleteResponse(f"Ответ {label} превышает допустимый размер.")
    try:
        result = json.loads(content)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"{label} вернул некорректный ответ API.") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{label} вернул некорректный ответ API.")
    return result


def list_models(name: str, key: str, *, base_url: str = "") -> list[dict]:
    provider = timeweb_provider(base_url) if name == "timeweb" else PROVIDERS[name]
    payload = api_request(provider, key, provider.list_path)
    models = {}
    display_names = dict(provider.models)
    for row in payload.get("models" if name == "gemini" else "data", []) or []:
        if not isinstance(row, dict):
            continue
        if name == "gemini":
            model = str(row.get("name", "")).removeprefix("models/")
            if "generateContent" not in (row.get("supportedGenerationMethods") or []):
                continue
            label = row.get("displayName")
        else:
            model, label = row.get("id"), row.get("name")
        if provider.allows(model):
            models[model] = {"id": model, "name": str(label or display_names.get(model, model))[:100]}
    # Keep the suggested models first, when the key can actually use them.
    return [models.pop(model) for model, _ in provider.models if model in models] + list(models.values())


@dataclass
class TextModel:
    provider: str
    key: str
    model: str
    max_input_chars: int
    max_output_tokens: int
    thinking: bool
    qwen_host: str = ""
    timeweb_base_url: str = ""

    def complete_json(self, system: str, payload: dict) -> dict:
        content = json.dumps(payload, ensure_ascii=False)
        if len(content) > self.max_input_chars:
            raise ValueError("Контекст превышает лимит. Увеличьте лимит входных данных в настройках.")
        provider = PROVIDERS[self.provider]
        if not provider.allows(self.model):
            raise ValueError("Выберите модель выбранного провайдера в настройках.")
        if self.provider == "qwen" and self.qwen_host:
            host = qwen_api_host(self.qwen_host)
            provider = replace(provider, base_url=f"https://{host}/compatible-mode/v1")
        if self.provider == "timeweb":
            provider = timeweb_provider(self.timeweb_base_url)
        path, body = self.generation_request(system, content)
        if self.provider == "timeweb":
            if payload.get("compact_review"):
                body["max_tokens"] = min(body["max_tokens"], 1600)
            elif payload.get("local_synthesis"):
                body["max_tokens"] = min(body["max_tokens"], 3200)
        run = current_run.get()
        job_id = run["id"] if run is not None else "-"
        for attempt in range(1, 3):
            start = time.monotonic()
            logger.info("provider_request id=%s provider=%s model=%s attempt=%s", job_id, self.provider, self.model, attempt)
            try:
                result = api_request(provider, self.key, path, body)
            except (ValueError, IncompleteResponse) as exc:
                logger.warning("provider_failed id=%s provider=%s type=%s http=%s phase=%s cause=%s elapsed_ms=%s",
                               job_id, self.provider, type(exc).__name__, getattr(exc.__cause__, "code", "-"),
                               getattr(exc, "phase", "-"), getattr(exc, "reason", type(exc.__cause__).__name__),
                               round((time.monotonic() - start) * 1000))
                raise
            usage = result.get("usageMetadata" if self.provider == "gemini" else "usage")
            token_counts = {k: v for k, v in (usage.items() if isinstance(usage, dict) else ())
                            if k in {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
                                     "promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount", "totalTokenCount"}
                            and type(v) is int}
            logger.info("provider_response id=%s provider=%s elapsed_ms=%s usage=%s",
                        job_id, self.provider, round((time.monotonic() - start) * 1000), token_counts)
            try:
                content = self.output_text(result)
                if self.provider == "timeweb":
                    fenced = re.fullmatch(r"\s*\x60{3}(?:json)?\s*(.*?)\s*\x60{3}\s*", content, re.I | re.S)
                    if fenced:
                        content = fenced.group(1)
                output = json.loads(content)
                if isinstance(output, dict):
                    return output
            except IncompleteResponse as exc:
                # Messages from output_text are fixed strings and never contain model output.
                logger.warning("provider_incomplete id=%s provider=%s reason=%s", job_id, self.provider, exc)
                raise
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                pass
            logger.warning("provider_invalid_json id=%s provider=%s attempt=%s", job_id, self.provider, attempt)
        raise ValueError(f"{provider.label} не вернул отчёт в формате JSON. Повторите анализ или выберите другую модель.")

    def generation_request(self, system: str, content: str) -> tuple[str, dict]:
        if self.provider == "gemini":
            if self.model.startswith("gemini-3"):
                reasoning = {"thinkingLevel": "high" if self.thinking else "low"}
            else:
                reasoning = {"thinkingBudget": 4096 if self.thinking else 1024}
            return f"/models/{self.model}:generateContent", {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": content}]}],
                "generationConfig": {
                    "responseMimeType": "application/json", "maxOutputTokens": self.max_output_tokens,
                    "thinkingConfig": reasoning,
                },
            }
        if self.provider == "openai":
            return "/responses", {
                "model": self.model, "instructions": system, "input": content, "store": False,
                "text": {"format": {"type": "json_object"}}, "max_output_tokens": self.max_output_tokens,
                "reasoning": {"effort": "medium" if self.thinking else "low"},
            }
        body = {
            "model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {"type": "json_object"}, "max_tokens": self.max_output_tokens,
        }
        if self.provider == "qwen":
            body["enable_thinking"] = self.thinking
        elif self.provider == "timeweb":
            # The gateway accepts the chat interface without provider-specific flags.
            body.pop("response_format")
            body["stream"] = True
            body["messages"][0]["content"] = system + "\nОтветь только валидным JSON-объектом без Markdown."
        else:
            body["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
        return "/chat/completions", body

    def output_text(self, result: dict) -> str:
        label = PROVIDERS[self.provider].label
        if self.provider == "gemini":
            if (result.get("promptFeedback") or {}).get("blockReason"):
                raise IncompleteResponse(f"{label} отклонил этот запрос.")
            candidate = result["candidates"][0]
            if candidate.get("finishReason") == "MAX_TOKENS":
                raise IncompleteResponse(f"Ответ {label} не поместился. Увеличьте лимит ответа.")
            if candidate.get("finishReason") not in (None, "STOP"):
                raise IncompleteResponse(f"{label} прервал генерацию ответа.")
            return "".join(part.get("text", "") for part in candidate["content"]["parts"] if not part.get("thought"))
        if self.provider == "openai":
            if result.get("status") == "incomplete":
                reason = (result.get("incomplete_details") or {}).get("reason")
                message = "Увеличьте лимит ответа." if reason == "max_output_tokens" else "Повторите запрос с другим контекстом."
                raise IncompleteResponse(f"{label} вернул неполный ответ. {message}")
            if result.get("status") != "completed":
                raise IncompleteResponse(f"{label} не завершил генерацию.")
            parts = [part for item in result["output"] if item.get("type") == "message" for part in item["content"]]
            if any(part.get("type") == "refusal" for part in parts):
                raise IncompleteResponse(f"{label} отказался обработать запрос.")
            return "".join(part["text"] for part in parts if part.get("type") == "output_text")
        choice = result["choices"][0]
        if choice.get("finish_reason") == "length":
            raise IncompleteResponse(f"Ответ {label} не поместился. Увеличьте лимит ответа.")
        return choice["message"]["content"]
