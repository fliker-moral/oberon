"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const content = $("#content");
const dialog = $("#dialog");
const views = { home: "Обзор", projects: "Проекты", create: "Новый проект", training: "Обучение", settings: "Настройки" };
const kinds = { manual: "Вручную", text: "Описание", site: "Сайт", github: "GitHub", local_git: "Локальный Git", miniapp: "Mini App", zip: "ZIP" };
const tabs = { report: "Отчёт", history: "История", plan: "План", offer: "КП" };
const statuses = { todo: "Запланировано", doing: "В работе", done: "Готово" };
const featureStatus = { confirmed: "Найдено в коде · без запуска", assumed: "Предположение", unverified: "Не проверено" };
const memoryScopes = [["similar", "Похожие проекты"], ["project", "Только этот проект"], ["global", "Все проекты"]];
const changeStatus = { initial: "Первый снимок", changed: "Есть изменения", unchanged: "Без изменений", site_changed: "Обновился сайт", ahead: "Есть новые коммиты", behind: "Новый SHA отстаёт от старого", diverged: "Ветки разошлись", identical: "Коммиты совпадают", snapshot_only: "Сравнение снимков" };
const fileStatus = { added: "Добавлен", removed: "Удалён", modified: "Изменён", renamed: "Переименован", copied: "Скопирован" };

const state = {
  view: "home", tab: "report", createKind: "manual", trainingKind: "github",
  project: null, review: null, comparison: null, bootstrap: null, models: {},
  query: "", offerDraft: null, loadSequence: 0,
};

function escapeHtml(value) {
  const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(value ?? "").replace(/[&<>"']/g, char => entities[char]);
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T") + "Z");
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat("ru", {
    dateStyle: "medium", timeStyle: "short",
  }).format(date);
}

function money(value) {
  return Number(value || 0).toLocaleString("ru-RU");
}

async function request(path, data, signal) {
  const options = { cache: "no-store", signal };
  if (data !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-Oberon-Token": state.bootstrap.request_token };
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Не удалось выполнить запрос");
  return result;
}

const post = (action, data) => request("/api/" + action, data);

let activeJob = null;
let displayedJob = null;
const jobStages = {
  collection: "сбор исходников", plan: "подготовка пакетов", map: "чтение пакетов",
  reduce: "сведение результатов", final: "итоговый отчёт", complete: "подготовка к сохранению", save: "сохранение",
};

function showJob(job) {
  displayedJob = job;
  $("#job-progress").hidden = false;
  const running = job.state === "running";
  const partialSaved = job.message === "Сохранён частичный коммерческий разбор";
  $("#job-message").textContent = job.state === "success" && !partialSaved
    ? "Результат сохранён" : job.message || "Подготовка проекта";
  const phase = job.state === "failed" && jobStages[job.stage];
  const providerWait = running && job.provider_phase && job.provider_started_ms &&
    (job.provider_phase === "waiting" ? "Timeweb · ожидание отклика" : "Timeweb · получение ответа") +
    " " + Math.max(0, Math.floor((Date.now() - job.provider_started_ms) / 1000)) + " с";
  $("#job-detail").textContent = [providerWait, phase && "Этап — " + phase, job.id && "Запуск " + job.id.slice(0, 8)]
    .filter(Boolean).join(" · ");
  $("#job-cancel").hidden = !running;
  $("#job-cancel").disabled = job.cancel_requested || ["waiting", "save"].includes(job.stage);
  $("#job-result").hidden = job.state !== "success";
  $("#job-dismiss").hidden = running;
  const bar = $("#job-bar");
  bar.hidden = !running;
  if (job.total > 0) {
    bar.max = job.total;
    bar.value = job.done;
  } else {
    bar.removeAttribute("value");
  }
}

async function waitForJob(id) {
  let missing = 0;
  while (true) {
    let status;
    try {
      status = await request("/api/progress?id=" + encodeURIComponent(id), undefined, AbortSignal.timeout(10000));
    } catch {
      showJob({ id, state: "running", stage: "waiting", message: "Восстанавливаем связь с Oberon…" });
    }
    if (status?.stage === "waiting") {
      if (++missing === 5) throw new Error("Запуск не найден. Повторите действие.");
    } else if (status) {
      missing = 0;
      showJob({ ...status, message: status.cancel_requested && status.state === "running"
        ? "Отмена после текущего запроса" : status.message });
      if (status.state !== "running") return status;
    }
    await new Promise(resolve => setTimeout(resolve, 700));
  }
}

function fetchJobResult(job) {
  const endpoint = ["train", "retrain"].includes(job.action) ? "training" : "project";
  return request("/api/" + endpoint + "?id=" + job.result_id);
}

async function followJob(id, start) {
  if (activeJob) throw new Error("Дождитесь окончания текущего анализа или отмените его");
  activeJob = id;
  showJob({ id, state: "running", stage: "waiting", message: "Подготовка проекта" });
  try {
    if (start) {
      try {
        await start();
      } catch (error) {
        if (!(error instanceof TypeError) && !(error instanceof SyntaxError)) throw error;
      }
    }
    return await waitForJob(id);
  } catch (error) {
    showJob({ id, state: "failed", message: error.message });
    throw error;
  } finally {
    activeJob = null;
  }
}

async function runAnalysis(action, data) {
  const id = crypto.randomUUID();
  const status = await followJob(id, () => post(action, { ...data, job_id: id, background: true }));
  if (status.state !== "success") throw new Error(status.message);
  return fetchJobResult(status);
}

$("#job-cancel").addEventListener("click", async event => {
  if (!activeJob) return;
  event.currentTarget.disabled = true;
  try {
    await post("cancel", { job_id: activeJob });
  } catch (error) {
    notify(error.message, true);
  }
});

$("#job-result").addEventListener("click", event => run(event.currentTarget, async () => {
  const job = displayedJob;
  const result = await fetchJobResult(job);
  await reload();
  if (["train", "retrain"].includes(job.action)) {
    state.review = result;
    navigate("training");
  } else {
    acceptProject(result, result.report.snapshot_only ? "history" : "report");
  }
}));

$("#job-dismiss").addEventListener("click", event => run(event.currentTarget, async () => {
  const job = displayedJob;
  await post("dismiss-job", { job_id: job.id });
  if (displayedJob === job) $("#job-progress").hidden = true;
}));

function notify(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("visible");
  node.classList.toggle("error", error);
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => node.classList.remove("visible"), error ? 8000 : 3400);
}

async function run(button, work) {
  const original = button?.textContent;
  if (button) {
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = "Выполняется…";
  }
  try {
    await work();
  } catch (error) {
    notify(error.message || "Действие не выполнено", true);
  } finally {
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

function button(label, action, css = "", value = "") {
  return `<button type="button" class="button ${css}" data-action="${action}" data-value="${escapeHtml(value)}">${escapeHtml(label)}</button>`;
}

function link(label, url) {
  return `<a class="button" href="${escapeHtml(url)}" download>${escapeHtml(label)}</a>`;
}

function pane(title, body, extra = "") {
  const heading = title ? `<div class="pane-head toolbar"><h2>${escapeHtml(title)}</h2>${extra}</div>` : "";
  return `<section class="pane">${heading}<div class="pane-body">${body}</div></section>`;
}

function page(title, actions = "", description = "") {
  return `<div class="page-head">
    <div><h1>${escapeHtml(title)}</h1>${description ? `<p>${escapeHtml(description)}</p>` : ""}</div>
    <div class="actions">${actions}</div>
  </div>`;
}

function empty(message) {
  return `<p class="empty">${escapeHtml(message)}</p>`;
}

function input(name, label, options = {}) {
  const { value = "", type = "text", required = false, placeholder = "", limit = 150, extra = "" } = options;
  return `<label class="field"><span>${escapeHtml(label)}</span>
    <input name="${name}" value="${escapeHtml(value)}" type="${type}" placeholder="${escapeHtml(placeholder)}"
      ${required ? "required" : ""} maxlength="${limit}" ${extra}>
  </label>`;
}

function textarea(name, label, options = {}) {
  const { value = "", limit = 6000, placeholder = "", className = "", required = false } = options;
  return `<label class="field"><span>${escapeHtml(label)}</span>
    <textarea name="${name}" maxlength="${limit}" placeholder="${escapeHtml(placeholder)}"
      class="${className}" ${required ? "required" : ""}>${escapeHtml(value)}</textarea>
  </label>`;
}

function select(name, label, entries, selected = "") {
  return `<label class="field"><span>${escapeHtml(label)}</span><select name="${name}">
    ${entries.map(([id, title]) => `<option value="${escapeHtml(id)}" ${String(id) === String(selected) ? "selected" : ""}>${escapeHtml(title)}</option>`).join("")}
  </select></label>`;
}

function checkbox(name, label, checked = false) {
  return `<label class="checkbox"><input type="checkbox" name="${name}" ${checked ? "checked" : ""}>${escapeHtml(label)}</label>`;
}

function submit(label) {
  return `<button type="submit" class="button primary">${escapeHtml(label)}</button>`;
}

function projectEntry(project) {
  return `<button class="entry ${state.project?.id === project.id ? "active" : ""}" data-action="project" data-value="${project.id}">
    <b>${escapeHtml(project.name)}</b>
    <small>${escapeHtml(kinds[project.kind] || project.kind)} · Версий ${project.revisions} · Задач ${project.pending}</small>
  </button>`;
}

function homeView() {
  const { projects, training } = state.bootstrap;
  const metrics = [
    ["Проекты", projects.length],
    ["Активные задачи", projects.reduce((total, project) => total + project.pending, 0)],
    ["Учебные разборы", training.length],
  ];
  return `<section class="hero">
    <div class="hero-body">
      <h1>Рабочее пространство</h1>
      <p>Найдите, какие функции можно предложить владельцу проекта, и соберите коммерческое предложение.</p>
      <div class="actions">${button("+ Новый проект", "new", "primary")}${button("Подключить Git", "new", "", "local_git")}</div>
    </div>
    <img src="/oberon.png" alt="">
  </section>
  <div class="metrics">${metrics.map(([label, count]) => `<div class="metric"><span>${label}</span><b>${count}</b></div>`).join("")}</div>
  <section class="pane">
    <div class="pane-head toolbar"><h2>Последние проекты</h2>${button("Все проекты", "navigate", "small", "projects")}</div>
    <div class="entry-list">${projects.length ? projects.slice(0, 6).map(projectEntry).join("") : empty("Создайте первый проект или импортируйте репозиторий.")}</div>
  </section>`;
}

function sourceTabs(options, current, action) {
  return `<div class="tabs source-tabs" aria-label="Источник">${options.map(kind =>
    `<button type="button" data-action="${action}" data-value="${kind}" aria-pressed="${current === kind}">${kinds[kind] || "Ссылка / описание"}</button>`
  ).join("")}</div>`;
}

function archiveField(label) {
  if (state.bootstrap.native) {
    return `<label class="field"><span>${escapeHtml(label)}</span>
      <div class="with-button"><input name="archive_path" readonly placeholder="Укажите ZIP на диске">
      ${button("Выбрать ZIP", "choose-archive")}</div>
    </label>`;
  }
  return `<label class="field"><span>${escapeHtml(label)} · до 64 МиБ в браузере</span>
    <input name="archive" type="file" accept=".zip,application/zip" required>
  </label>`;
}

function sourceForm(kind, training = false) {
  if (kind === "manual") {
    return `<form class="stack" data-form="manual">
      ${input("name", "Название", { required: true, placeholder: "Например, студия дизайна" })}
      ${textarea("description", "Описание проекта", { className: "large-text" })}
      <div class="actions">${submit("Создать проект")}</div>
    </form>`;
  }
  let fields;
  if (kind === "local_git") {
    const choose = state.bootstrap.native ? button("Выбрать папку", "choose-folder") : "";
    fields = `<div class="with-button">
      ${input("path", "Папка репозитория", { required: true, placeholder: "C:\\Projects\\my-app", limit: 1000 })}
      <div class="actions">${choose}</div>
    </div>
    ${input("branch", "Ветка, тег или SHA", { placeholder: "HEAD", limit: 120 })}
    <p class="fine">Читаются коммиты. ${state.bootstrap.git_available ? "Git для Windows найден." : "Для локальных репозиториев установите Git для Windows."}</p>`;
  } else if (kind === "zip") {
    fields = archiveField("ZIP-архив проекта");
  } else if (kind === "auto") {
    fields = textarea("text", "Описание бизнеса или HTTPS-ссылка", { required: true, className: "large-text", placeholder: "Что продаёт проект, кому и через какие каналы" });
  } else {
    const github = kind === "github";
    fields = input("text", github ? "Ссылка на репозиторий" : "Прямая ссылка на фронтенд", {
      type: "url", required: true, limit: 2000, placeholder: github ? "https://github.com/owner/repo" : "https://app.example.com/",
    });
    if (github) {
      fields += input("branch", "Ветка, тег или SHA", { placeholder: "Ветка по умолчанию", limit: 120 });
      fields += `<p class="fine">Для приватного репозитория добавьте токен GitHub в настройках.</p>`;
    } else {
      fields += `<p class="fine">Закрытые экраны внутри Telegram по публичному URL недоступны.</p>`;
    }
  }
  fields += textarea("description", "Коммерческая цель (необязательно)", {
    placeholder: "Например, увеличить оплаченные заказы из Telegram или видеть конверсию по каждому товару",
  });
  if (!training) fields += input("name_override", "Название проекта (необязательно)");
  return `<form class="stack" data-form="${training ? "train" : "analyze"}" data-kind="${kind}">
    ${fields}
    <p class="send-note">${escapeHtml(activeProvider().label)} получит подходящий текст проекта. Большие проекты обрабатываются несколькими запросами.</p>
    <div class="actions">${submit(training ? "Найти идеи для оценки" : "Найти идеи для КП")}
      ${!training && ["github", "local_git", "zip"].includes(kind) ?
        '<button type="submit" class="button" value="snapshot">Сохранить снимок без AI</button>' : ""}
    </div>
  </form>`;
}

function missingKey() {
  if (!activeProvider().missingKey) return "";
  return `<div class="notice toolbar"><p>Для анализа добавьте ключ ${escapeHtml(activeProvider().label)}.</p>
    ${button("Открыть настройки", "navigate", "small", "settings")}</div>`;
}

function createView() {
  const kind = state.createKind;
  return page("Новый проект", button("К проектам", "navigate", "", "projects")) +
    `<div class="stack">${kind !== "manual" ? missingKey() : ""}${pane("", sourceTabs(
      ["manual", "auto", "github", "local_git", "zip", "miniapp"], kind, "create-kind",
    ) + sourceForm(kind))}</div>`;
}

function projectListBody() {
  const query = state.query.trim().toLocaleLowerCase("ru");
  const selected = state.bootstrap.projects.filter(project =>
    (project.name + " " + (kinds[project.kind] || project.kind)).toLocaleLowerCase("ru").includes(query)
  );
  return selected.length ? selected.map(projectEntry).join("") :
    empty(query ? "По этому запросу ничего не найдено." : "Проектов пока нет.");
}

function projectView() {
  return page("Проекты", button("+ Новый проект", "new", "primary")) + `<div class="split">
    <aside class="pane">
      <div class="search-wrap">${input("query", "Поиск", { type: "search", value: state.query, placeholder: "Название или источник" })}</div>
      <div class="entry-list" id="project-list">${projectListBody()}</div>
    </aside>
    <div id="project-detail">${state.project ? projectDetail(state.project) : pane("", empty("Выберите проект, чтобы открыть рабочее пространство."))}</div>
  </div>`;
}

function projectDetail(project) {
  const report = project.report;
  const kind = project.source.kind || report.source_kind;
  const ref = report.version_ref;
  const source = project.source.url || project.source.path || "";
  const info = [kinds[kind] || kind, ref?.slice(0, 12), report.model].filter(Boolean);
  const navigation = Object.entries(tabs).map(([id, title]) =>
    `<button type="button" data-action="project-tab" data-value="${id}" aria-pressed="${state.tab === id}">${title}</button>`
  ).join("");
  const parts = { report: reportView, history: historyView, plan: planView, offer: offerView };
  return `<div class="stack">
    <section class="pane">
      <div class="project-head">
        <div class="pills">${info.map(value => `<span class="pill">${escapeHtml(value)}</span>`).join("")}</div>
        <h2>${escapeHtml(project.name)}</h2>
        ${source ? `<div class="fine break">${escapeHtml(source)}${project.source.branch ? " · " + escapeHtml(project.source.branch) : ""}</div>` : ""}
        <div class="actions">
          ${button(kind === "manual" ? "Анализировать" : "Обновить анализ", "refresh", "primary")}
          ${link("Экспорт отчёта", "/api/export?id=" + project.id)}
          ${button("Редактировать", "edit-project")}
          ${button("Удалить", "delete-project", "danger")}
        </div>
      </div>
      <div class="tabs" aria-label="Проект">${navigation}</div>
    </section>
    ${parts[state.tab](project)}
  </div>`;
}

function recommendations(project) {
  const report = project.report;
  return report.recommendations.map((rec, index) => {
    const priority = { high: "Высокий", medium: "Средний", low: "Низкий" }[rec.priority] || "Средний";
    const hours = Number.isFinite(rec.hours_min) ? ` · ${rec.hours_min}–${rec.hours_max} ч` : "";
    return `<article class="recommendation">
      <label class="rec-title"><input type="checkbox" name="recommendation" value="${index}" checked>
        <strong>${escapeHtml(rec.title)}</strong>
      </label>
      <p>${escapeHtml(rec.description)}</p>
      ${commercialValue(rec)}
      ${decisionView(rec)}
      ${rec.tasks?.length ? `<details><summary>Состав работ</summary><ul class="plain-list">${rec.tasks.map(task => `<li>${escapeHtml(task)}</li>`).join("")}</ul></details>` : ""}
      <div class="toolbar">
        <span><span class="rec-price">${estimateText(rec, report.currency)}</span> <span class="fine">${hours} · ${priority} приоритет</span></span>
        <div class="actions">${button("Текст для клиента", "project-client-text", "small", index)}${button("В план", "add-recommendation", "small", index)}${button("Цена", "price", "small", index)}</div>
      </div>
    </article>`;
  }).join("");
}

function commercialValue(item) {
  return (item.business_value ? `<p><strong>Польза для бизнеса</strong> · ${escapeHtml(item.business_value)}</p>` : "") +
    (item.success_metric ? `<p><strong>Как измерить результат</strong> · ${escapeHtml(item.success_metric)}</p>` : "");
}

function estimateText(item, currency) {
  return Number.isFinite(item.cost_min) && Number.isFinite(item.cost_max)
    ? `${money(item.cost_min)}–${money(item.cost_max)} ${escapeHtml(currency)}`
    : "Стоимость уточняется";
}

function featureList(report) {
  if (!report.existing_features.length) return empty("Функции пока не зафиксированы.");
  return `<ul class="feature-list">${report.existing_features.map(feature => `<li>
    ${escapeHtml(feature.title)} <span class="fine">· ${escapeHtml(featureStatus[feature.status] || feature.status)}
    ${feature.source ? " · " + escapeHtml(feature.source) : ""}</span>${citationsView(feature.citations)}
  </li>`).join("")}</ul>`;
}

function citationsView(citations = []) {
  if (!citations.length) return "";
  return `<details class="evidence"><summary>Источники · ${citations.length}</summary>${citations.map(c =>
    `<div class="source-quote"><b class="mono">${escapeHtml(c.path)}${c.start_line ? ":" + c.start_line + (c.end_line !== c.start_line ? "–" + c.end_line : "") : ""}</b><pre>${escapeHtml(c.quote)}</pre></div>`
  ).join("")}</details>`;
}

function decisionView(item) {
  const confidence = { high: "высокая", medium: "средняя", low: "низкая" }[item.confidence] || "не указана";
  const cited = item.citations?.length;
  const groups = [["acceptance_criteria", "Как проверить результат"], ["assumptions", "Условия оценки"], ["risks", "Риски и зависимости"]]
    .filter(([key]) => item[key]?.length);
  return `<div class="decision-detail">
    <div class="pills"><span class="pill ${cited ? "" : "hot"}">${cited ? "Цитата найдена" : "Гипотеза · нужна проверка"}</span><span class="fine">Уверенность модели: ${confidence}</span></div>
    ${item.evidence ? `<p class="fine">Основание модели: ${escapeHtml(item.evidence)}</p>` : ""}
    ${citationsView(item.citations)}
    ${groups.length ? `<details><summary>Проверка и условия</summary>${groups.map(([key, label]) => `<h4>${label}</h4><ul class="plain-list">${item[key].map(text => `<li>${escapeHtml(text)}</li>`).join("")}</ul>`).join("")}</details>` : ""}
  </div>`;
}

function questionsView(questions = []) {
  return questions.length ? pane("Что уточнить до оценки работ", `<ul class="plain-list">${questions.map(q => `<li>${escapeHtml(q)}</li>`).join("")}</ul>`) : "";
}

function auditExcerpts(excerpts, page = 0) {
  const perPage = 20;
  const pages = Math.ceil(excerpts.length / perPage);
  const preview = excerpts.slice(page * perPage, (page + 1) * perPage).map(e =>
    `<details class="source-quote"><summary><span class="mono">${escapeHtml(e.id + " · " + e.path)}${e.start_line ? ":" + e.start_line + "–" + e.end_line : ""}</span></summary><pre>${escapeHtml(e.content)}</pre></details>`
  ).join("");
  const navigation = pages < 2 ? "" : `<div class="toolbar excerpt-navigation">
    <button type="button" class="button small" data-action="audit-page" data-value="${page - 1}" ${page === 0 ? "disabled" : ""}>Назад</button>
    <span class="fine">Страница ${page + 1} из ${pages}</span>
    <button type="button" class="button small" data-action="audit-page" data-value="${page + 1}" ${page + 1 === pages ? "disabled" : ""}>Далее</button>
  </div>`;
  return navigation + preview;
}

function auditView(audit, quality = {}) {
  if (!audit?.source_excerpts) return "";
  const map = audit.project_map || {};
  const memory = audit.feedback_selected || [];
  const exclusionNames = { cache_or_dependencies: "Кэши и зависимости", generated: "Сборочный мусор", unsupported_or_binary: "Бинарные и неподдерживаемые", secret: "Файлы с секретами", link: "Ссылки и подмодули", unsafe: "Некорректные пути" };
  const excluded = Object.entries(audit.excluded || {});
  const indexed = audit.coverage_mode === "complete";
  const partial = audit.coverage_mode === "partial";
  return pane("Основания анализа", `<div class="stack audit">
    <div class="audit-metrics"><div><b>${audit.analyzed_files} / ${audit.eligible_files}</b><span>${partial ? "файлов с полученными фрагментами" : "файлов в контексте"}</span></div><div><b>${audit.pages || 0}</b><span>страниц</span></div><div><b>${quality.cited_ideas || 0} / ${(quality.cited_ideas || 0) + (quality.hypotheses || 0)}</b><span>идей с цитатами</span></div></div>
    <p class="fine">${escapeHtml(quality.note || "Цитаты сверяются с фрагментами источника. Код проекта не запускался.")}</p>
    ${map.dependencies?.length ? `<p class="fine">Объявленные зависимости: ${escapeHtml(map.dependencies.join(", "))}</p>` : ""}
    ${audit.total_batches ? `<p class="fine">Пакеты обработаны: ${audit.completed_batches} из ${audit.total_batches} · запросов к модели: ${audit.requests}${audit.reused_requests ? ` · восстановлено ответов: ${audit.reused_requests}` : ""} · символов исходников: ${money(audit.characters_processed)}</p>` : ""}
    ${audit.synthesis === "local_recovery" ? '<p class="fine">Итог восстановлен из ответов по пакетам.</p>' : ""}
    ${map.sections?.length ? `<details><summary>Карта проекта</summary><ul class="plain-list fine">${map.sections.map(s =>
      `<li><strong>${escapeHtml(s.name)}</strong> · файлов ${s.files} · ${escapeHtml(s.examples.join(", "))}</li>`).join("")}</ul></details>` : ""}
    ${["local_recovery", "partial_local"].includes(audit.synthesis) && audit.batch_reviews?.length ? `<details><summary>Заметки по пакетам · ${audit.batch_reviews.length}</summary>
      ${audit.batch_reviews.map(b => `<details><summary>Пакет ${b.batch}</summary><ul class="plain-list fine">${b.notes.map(n =>
        `<li>${n.title ? `<strong>${escapeHtml(n.title)}</strong> · ` : ""}${escapeHtml([n.summary, n.observation, n.proposal].filter(Boolean).join(" "))}</li>`).join("")}</ul></details>`).join("")}</details>` : ""}
    ${excluded.length ? `<details><summary>Исключено файлов · ${excluded.reduce((sum, [, n]) => sum + n, 0)}</summary><ul class="plain-list fine">${excluded.map(([key, n]) => `<li>${escapeHtml(exclusionNames[key] || key)} · ${n}</li>`).join("")}</ul></details>` : ""}
    <details><summary>Что получила модель · ${audit.excerpts} фрагментов</summary>
      <p class="fine">${indexed ? "Все подходящие исходники обработаны пакетами. Цитаты доступны в карточках решений." : partial ? "Показаны фрагменты, по которым получен ответ." : "Разбор содержит выборку переданных фрагментов."}</p>
      <div data-audit-excerpts>${auditExcerpts(audit.source_excerpts)}</div>
    </details>
    ${audit.files_not_sent?.length ? `<details><summary>Файлы вне контекста · первые ${audit.files_not_sent.length}</summary><ul class="plain-list fine">${audit.files_not_sent.map(path => `<li>${escapeHtml(path)}</li>`).join("")}</ul></details>` : ""}
    <details><summary>Оценки владельца в контексте · ${memory.length}</summary>
      ${memory.length ? memory.map(m => `<div class="memory-note"><strong>${escapeHtml(m.idea)}</strong> <span class="fine">· ${m.rating ? m.rating + " / 5" : escapeHtml({ good: "принято", bad: "отклонено", own: "своя идея" }[m.verdict])}</span><p>${escapeHtml(m.reason_or_own_idea)}</p>${m.correction ? `<p><b>Ваш вариант</b> · ${escapeHtml(m.correction)}</p>` : ""}<small>${escapeHtml(m.selection_reason)}</small></div>`).join("") : '<p class="fine">Подходящих активных оценок нет или использование обучения отключено.</p>'}
    </details>
  </div>`);
}

function reportView(project) {
  const report = project.report;
  const body = report.recommendations.length ?
    `<form class="stack" data-form="offer">${recommendations(project)}
      <div class="actions">${submit("Сформировать КП по выбранному")}</div></form>` :
    empty("Предложений пока нет. Уточните цель проекта или добавьте свою идею в план.");
  const limitations = report.limitations.length ?
    pane("Ограничения анализа", `<ul class="plain-list fine">${report.limitations.map(note => `<li>${escapeHtml(note)}</li>`).join("")}</ul>`) : "";
  return partialNotice(report.audit) + pane("О бизнесе", `<p class="break">${escapeHtml(report.summary || "Описание можно добавить при редактировании проекта.")}</p>`) +
    pane("Что предложить клиенту", body) + questionsView(report.questions) + pane("Что уже есть в проекте", featureList(report)) +
    auditView(report.audit, report.quality) + limitations;
}

function partialNotice(audit) {
  if (audit?.coverage_mode !== "partial") return "";
  return `<div class="notice"><p><strong>Частичный разбор</strong> · пакетов ${audit.completed_batches} из ${audit.total_batches}.</p>
    <p>Можно собрать КП из готовых идей. Чтобы продолжить полный разбор, повторите тот же запуск с тем же источником и настройками.</p></div>`;
}

function diffView(change) {
  if (!change || change.status === "initial") return empty("Сначала сохраните две версии или выберите коммиты.");
  const delta = change.analysis_delta || {};
  const list = [
    ["Функции появились в отчёте", delta.new_features],
    ["Функции больше не перечислены", delta.removed_features],
    ["Новые рекомендации", delta.new_recommendations],
    ["Снятые рекомендации", delta.removed_recommendations],
  ].filter(([, values]) => values?.length);
  const files = (change.files || []).map(file => {
    const row = `${escapeHtml(file.path)} <span class="fine">· ${escapeHtml(fileStatus[file.status] || file.status)}
      ${file.additions === undefined ? "" : `· +${file.additions}/−${file.deletions}`}</span>`;
    return file.patch ?
      `<details class="diff-item"><summary>${row}</summary><pre>${escapeHtml(file.patch)}</pre></details>` :
      `<div class="diff-item">${row}</div>`;
  }).join("");
  const range = change.base && change.head ? `<span class="mono fine">${escapeHtml(change.base.slice(0, 12))} → ${escapeHtml(change.head.slice(0, 12))}</span>` : "";
  return `<div class="stack">
    <div class="pills"><span class="pill hot">${escapeHtml(changeStatus[change.status] || change.status)}</span>${range}</div>
    ${list.length ? `<ul class="plain-list">${list.map(([label, values]) => `<li><b>${label}</b> · ${escapeHtml(values.join(", "))}</li>`).join("")}</ul>` : ""}
    ${change.commits?.length ? `<details><summary>Коммиты в диапазоне (${change.total_commits ?? change.commits.length})</summary>${commitsView(change.commits)}</details>` : ""}
    <div>${files || '<span class="fine">Файловых изменений в этом сравнении нет.</span>'}</div>
    ${change.limitations?.length ? `<ul class="plain-list fine">${change.limitations.map(note => `<li>${escapeHtml(note)}</li>`).join("")}</ul>` : ""}
  </div>`;
}

function commitsView(commits) {
  return commits.map(commit => `<div class="commit">
    <b class="mono">${escapeHtml(commit.sha.slice(0, 12))}</b> ${escapeHtml(commit.message)}
    <span class="fine">${escapeHtml(formatDate(commit.date))}</span>
  </div>`).join("");
}

function historyView(project) {
  const reverse = project.versions.slice().reverse();
  const subtitle = version => `#${version.id} · ${version.ref.slice(0, 10)}`;
  const compare = reverse.length < 2 ? "" : `<form class="stack" data-form="compare">
    <div class="field-grid">
      ${select("old_id", "Старая версия", reverse.slice(0, -1).map(version => [version.id, subtitle(version)]), state.comparison?.old_id)}
      ${select("new_id", "Новая версия", project.versions.slice(0, -1).map(version => [version.id, subtitle(version)]), state.comparison?.new_id)}
    </div>
    <div class="actions">${submit("Сравнить версии")}</div>
  </form>`;
  const versionList = project.versions.map(version => `<div class="commit toolbar">
    <span>${escapeHtml(subtitle(version))} <span class="fine">· ${escapeHtml(formatDate(version.created_at))}</span></span>
    ${button("Открыть отчёт", "version", "small", version.id)}
  </div>`).join("");
  const git = project.commits.length ? pane("Коммиты Git", commitsView(project.commits) + `<form class="stack" data-form="git-diff">
    <datalist id="commit-shas">${project.commits.map(commit => `<option value="${escapeHtml(commit.sha)}">${escapeHtml(commit.message)}</option>`).join("")}</datalist>
    <div class="field-grid">
        ${input("base", "Старый SHA", { required: true, limit: 64, extra: 'list="commit-shas"', value: state.comparison?.base || project.commits[1]?.sha || "" })}
        ${input("head", "Новый SHA", { required: true, limit: 64, extra: 'list="commit-shas"', value: state.comparison?.head || project.commits[0]?.sha || "" })}
    </div>
    <div class="actions">${submit("Показать diff")}</div>
  </form>`) : "";
  return pane("Сохранённые версии", (versionList || empty("Отчётов пока нет.")) + compare) + git +
    `<div id="comparison">${pane("Изменения", diffView(state.comparison?.result || project.changes))}</div>`;
}

function planView(project) {
  const items = project.items;
  const sections = Object.entries(statuses).filter(([status]) => items.some(item => item.status === status));
  return pane("План работ", `<div class="stack">
    ${items.length ? sections.map(([status, label]) => `<section class="stack"><h3>${label}</h3>
      ${items.filter(item => item.status === status).map(item => `<article class="task">
        <div class="toolbar"><strong>${escapeHtml(item.title)}</strong>${button("Править", "edit-task", "small", item.id)}</div>
        ${item.details ? `<p class="fine">${escapeHtml(item.details)}</p>` : ""}
        <div class="actions">
          ${status === "todo" ? button("В работу", "task-status", "small", item.id + ":doing") : ""}
          ${status !== "done" ? button("Готово", "task-status", "small", item.id + ":done") : button("Вернуть в план", "task-status", "small", item.id + ":todo")}
        </div>
      </article>`).join("")}
    </section>`).join("") : empty("Добавьте задачу вручную или перенесите её из отчёта.")}
  </div>`, button("+ Задача", "edit-task", "primary small"));
}

function offerView(project) {
  return pane("Предложение клиенту", `<form class="stack" data-form="save-offer">
    ${textarea("text", "Черновик", { value: state.offerDraft ?? project.offer, limit: 30000, className: "offer-text", placeholder: "Напишите предложение или сформируйте его из рекомендаций в отчёте." })}
    <div class="actions">${submit("Сохранить черновик")}${button("Скопировать", "copy-offer")}
      ${project.offer ? link("Выгрузить TXT", ` /api/export?id=${project.id}&kind=offer`.trim()) : ""}</div>
  </form>`);
}

function trainingView() {
  const kind = state.trainingKind;
  const list = state.bootstrap.training.map(row => `<button class="entry ${state.review?.id === row.id ? "active" : ""}"
    data-action="review" data-value="${row.id}"><b>${escapeHtml(row.summary)}</b>
    <small>${escapeHtml(row.source)} · Оценок ${row.reviews}</small></button>`).join("");
  return page("Обучение", "", "Оцените коммерческие идеи и сохраните свои варианты для следующих проектов.") +
    `<div class="stack">${missingKey()}<div class="split">
      <section class="pane"><div class="pane-head"><h2>Прошлые разборы</h2></div><div class="entry-list">
        ${list || empty("Разборов пока нет.")}
      </div></section>
      ${pane("Новый разбор", sourceTabs(["github", "local_git", "zip"], kind, "training-kind") + sourceForm(kind, true))}
    </div>
    ${state.review ? reviewView(state.review) : ""}
  </div>`;
}

function reviewView(review) {
  const metadata = review.metadata || {};
  const hasOfferIdeas = review.ideas.some(idea => idea.description && idea.business_value && idea.success_metric);
  const feedback = new Map(review.feedback.filter(item => item.idea_index >= 0).reverse().map(item => [item.idea_index, item]));
  const cards = review.ideas.map((idea, index) => {
    const current = feedback.get(index);
    const stateLabel = current?.rating ? current.rating + " из 5" : current?.verdict === "good" ? "Принята" : "Отклонена";
    return `<article class="review" id="review-idea-${index}">
      <div class="toolbar"><h3>${escapeHtml(idea.title)}</h3>${current ? `<span class="pill hot">${stateLabel}${current.enabled ? "" : " · отключена"}</span>` : ""}</div>
      ${idea.description ? `<p>${escapeHtml(idea.description)}</p>` : ""}
      ${idea.reason ? `<p>${escapeHtml(idea.reason)}</p>` : ""}
      ${commercialValue(idea)}
      ${decisionView(idea)}
      ${idea.description && idea.business_value && idea.success_metric
        ? `<div class="actions">${button("Текст для клиента", "training-client-text", "small", index)}</div>` : ""}
      <form data-form="feedback" data-index="${index}">
        <div class="field-grid">
          ${select("rating", "Оценка решения", [["", "Выберите оценку"], ["1", "1 · Не подходит"], ["2", "2 · Слабое решение"], ["3", "3 · Требует доработки"], ["4", "4 · Хорошее решение"], ["5", "5 · Отличное решение"]], current?.rating || "")}
          ${select("scope", "Где учитывать решение", memoryScopes, current?.scope || "similar")}
        </div>
        ${textarea("comment", "Почему такая оценка", { value: current?.comment || "", limit: 1000 })}
        ${textarea("correction", "Ваш вариант решения", { value: current?.correction || "", limit: 1500 })}
        <div class="actions">${submit("Сохранить оценку")}</div>
      </form>
    </article>`;
  }).join("");
  const memory = review.feedback.map(item => `<article class="memory-note">
    <div class="toolbar"><strong>${escapeHtml(item.verdict === "own" ? "Своя идея" : review.ideas[item.idea_index]?.title || "Оценка")}</strong><span class="pill ${item.enabled ? "" : "hot"}">${item.enabled ? "Активна" : "Отключена"}</span></div>
    <p>${escapeHtml(item.comment)}</p><p class="fine">${escapeHtml(memoryScopes.find(([id]) => id === item.scope)?.[1] || item.scope)}</p>
    ${item.rating ? `<p class="fine">Ваша оценка · ${item.rating} из 5</p>` : ""}
    ${item.correction ? `<p><b>Ваш вариант</b> · ${escapeHtml(item.correction)}</p>` : ""}
    <div class="actions">${button("Изменить", "memory-edit", "small", item.id)}${button(item.enabled ? "Не учитывать" : "Учитывать", "memory-toggle", "small", item.id)}${button("Удалить", "memory-delete", "danger small", item.id)}</div>
  </article>`).join("");
  return `<div id="active-review" class="stack">` + partialNotice(metadata) + pane("Оценка идей", `<div class="stack">
    <p class="break">${escapeHtml(review.summary)}</p>
    ${hasOfferIdeas ? `<div class="actions">${button("Собрать КП из идей", "training-offer", "primary")}</div>` : ""}
    <p class="fine">Оценено ${feedback.size} из ${review.ideas.length}. Оценки и ваши варианты используются в следующих разборах.</p>
    ${metadata.parent_training_id ? `<p class="fine">Пересмотр разбора #${metadata.parent_training_id} на сохранённом снимке.</p>` : ""}
    ${metadata.model ? `<p class="fine">${escapeHtml(metadata.model)} · ${escapeHtml(metadata.version_ref?.slice(0, 12) || "")}</p>` : ""}
    ${cards}
    <form class="stack" data-form="own-idea">
      ${textarea("comment", "Своя идея", { required: true, limit: 1000, placeholder: "Что бы вы предложили для этого проекта?" })}
      ${select("scope", "Где учитывать идею", memoryScopes, "similar")}
      <div class="actions">${submit("Сохранить идею")}</div>
    </form>
    <p class="fine">«Только этот проект» связывается с адресом Git, локальной папкой или именем ZIP. Для общего правила выберите «Все проекты».</p>
    ${metadata.source_excerpts?.length ? `<div class="actions">${button("Пересмотреть идеи с учётом оценок", "retrain", "primary")}</div><p class="fine">Будет создан отдельный разбор на сохранённых фрагментах с запросом к выбранной модели.</p>` : ""}
  </div>`, button("Удалить разбор", "delete-review", "danger small")) +
    auditView(metadata, metadata.quality) + questionsView(metadata.questions) +
    (memory ? pane("Память этого разбора", `<div class="stack">${memory}</div>`) : "") +
    (metadata.limitations?.length ? pane("Ограничения разбора", `<ul class="plain-list fine">${metadata.limitations.map(l => `<li>${escapeHtml(l)}</li>`).join("")}</ul>`) : "") + "</div>";
}

function activeProvider() {
  const { settings, providers } = state.bootstrap;
  return { ...providers[settings.provider], missingKey: settings[settings.provider] === "missing" };
}

function availableModels(provider) {
  return state.models[provider] || state.bootstrap.providers[provider].models;
}

function modelOptions(provider, selected) {
  const models = [...availableModels(provider)];
  if (!models.some(model => model.id === selected)) models.push({ id: selected, name: selected });
  return models.map(model => [model.id, model.name]);
}

function populateModels(provider, selected) {
  const model = $('[name="model"]');
  model.replaceChildren(
    ...modelOptions(provider, selected).map(([id, title]) => new Option(title, id, false, id === selected)),
  );
}

function keySettings(service, name, label, checkLabel = "Проверить ключ") {
  const status = state.bootstrap.settings[service];
  const hint = { stored: "Сохранён в Windows", env: "Берётся из окружения", missing: "Не задан" }[status];
  return `<div class="stack">
    ${input(name, label, { type: "password", placeholder: hint, limit: 300, extra: 'autocomplete="new-password"' })}
    <div class="actions">
      ${button(checkLabel, "check", "small", service)}
      ${status === "stored" ? button("Удалить ключ", "clear-key", "danger small", name) : ""}
      <span class="fine" id="connection-${service}">${hint}</span>
    </div>
  </div>`;
}

function settingsView() {
  const settings = state.bootstrap.settings;
  const providers = Object.entries(state.bootstrap.providers);
  const labels = { deepseek: "Ключ DeepSeek API", gemini: "Ключ Gemini API", openai: "Ключ OpenAI API", qwen: "Ключ API Alibaba Cloud Model Studio", timeweb: "Ключ API Timeweb AI Gateway" };
  const help = {
    deepseek: "Ключ создаётся в кабинете DeepSeek API.",
    gemini: "Ключ создаётся в Google AI Studio.",
    openai: "Подключение использует OpenAI API с отдельным ключом и оплатой API. Доступ из подписки ChatGPT на Oberon не переносится.",
    qwen: "Скопируйте ключ и API Host из Alibaba Cloud Model Studio. Сохраните настройки перед проверкой. Проверка отправляет короткий запрос к выбранной модели.",
    timeweb: "Сохраните ключ и базовый URL из Timeweb AI Gateway. Проверка ключа обновляет список доступных моделей Qwen.",
  };
  return page("Настройки") + `<div class="stack">${pane("", `<form data-form="settings">
    <section class="settings-group stack">
      <h2>Модели</h2>
      <div class="field-grid">
        ${select("provider", "Провайдер", providers.map(([id, spec]) => [id, spec.label]), settings.provider)}
        ${select("model", "Модель анализа", modelOptions(settings.provider, settings.model), settings.model)}
      </div>
      ${providers.map(([id]) => `<div class="stack" data-provider-panel="${id}" ${id === settings.provider ? "" : "hidden"}>
        <p class="fine">${help[id]}</p>
        ${id === "qwen" ? input("qwen_host", "API Host для Qwen", { value: settings.qwen_host, placeholder: "dashscope-intl.aliyuncs.com (Сингапур)", limit: 250 }) : ""}
        ${id === "timeweb" ? input("timeweb_base_url", "Базовый URL Timeweb AI", { value: settings.timeweb_base_url, limit: 250 }) : ""}
        ${keySettings(id, id + "_key", labels[id], id === "qwen" ? "Проверить подключение" : undefined)}
      </div>`).join("")}
      <div class="field-grid">
        ${input("max_input_chars", "Лимит контекста, символов", { type: "number", value: settings.max_input_chars, required: true, extra: "min=8000 max=64000" })}
        ${input("max_output_tokens", "Лимит ответа, токенов", { type: "number", value: settings.max_output_tokens, required: true, extra: "min=1500 max=32000" })}
      </div>
      ${checkbox("thinking", "Усилить рассуждение", settings.thinking)}
      ${checkbox("use_feedback", "Учитывать мои оценки и идеи в новых анализах", settings.use_feedback)}
    </section>
    <section class="settings-group stack">
      <h2>GitHub</h2>
      <p class="fine">Для приватных проектов используйте токен с правом чтения Contents нужного репозитория.</p>
      ${keySettings("github", "github_token", "Токен GitHub")}
    </section>
    <section class="settings-group stack">
      <h2>Расчёт смет</h2>
      <div class="field-grid">
        ${input("hourly_rate", "Ставка за час", { type: "number", value: settings.hourly_rate, required: true, extra: "min=1 max=1000000" })}
        ${input("minimum_price", "Минимальная стоимость", { type: "number", value: settings.minimum_price, required: true, extra: "min=0 max=1000000000" })}
      </div>
      ${input("currency", "Валюта", { limit: 8, required: true, value: settings.currency })}
    </section>
    <section class="settings-group actions">${submit("Сохранить настройки")}</section>
  </form>`)}
  ${pane("Данные", '<p class="fine">Резервная копия включает проекты, историю, планы и обучение. Ключи API из копии исключены.</p>' +
    link("Скачать резервную копию", "/api/backup"))}
  </div>`;
}

function modelStatus() {
  const { settings, providers } = state.bootstrap;
  const hasKey = Object.keys(providers).some(provider => ["stored", "env"].includes(settings[provider]));
  if (!hasKey) return "Ключи API не добавлены";
  const { provider, model } = settings;
  return availableModels(provider).find(option => option.id === model)?.name || model;
}

function render(scrollTop = false) {
  const renderers = { home: homeView, create: createView, projects: projectView, training: trainingView, settings: settingsView };
  content.innerHTML = renderers[state.view]();
  content.setAttribute("aria-busy", "false");
  $("#location").textContent = views[state.view];
  $("#active-model").textContent = modelStatus();
  document.querySelectorAll("[data-nav]").forEach(element => {
    const activeView = state.view === "create" ? "projects" : state.view;
    if (element.dataset.nav === activeView) element.setAttribute("aria-current", "page");
    else element.removeAttribute("aria-current");
  });
  if (scrollTop) window.scrollTo(0, 0);
}

function navigate(view) {
  state.view = view;
  state.loadSequence += 1;
  render(true);
}

async function reload() {
  state.bootstrap = await request("/api/bootstrap");
}

function keepDraft() {
  const editor = $('[data-form="save-offer"] textarea');
  if (editor) state.offerDraft = editor.value;
}

function acceptProject(project, tab = state.tab) {
  const switching = state.project?.id !== project.id;
  state.project = project;
  if (switching) {
    state.offerDraft = null;
    state.comparison = null;
  }
  state.tab = tab;
  navigate("projects");
}

async function openProject(id) {
  keepDraft();
  const order = ++state.loadSequence;
  const project = await request("/api/project?id=" + id);
  if (order === state.loadSequence) acceptProject(project, "report");
}

function showDialog(title, html) {
  dialog.innerHTML = `<div class="toolbar dialog-title"><h2>${escapeHtml(title)}</h2>${button("Закрыть", "close-dialog", "small")}</div>${html}`;
  if (!dialog.open) dialog.showModal();
}

function showClientText(value) {
  showDialog("Текст для клиента", `<div id="client-offer-draft" class="stack">
    ${textarea("text", "Предложение клиенту", { value, limit: 30000, className: "offer-text" })}
    <div class="actions">${button("Скопировать", "copy-offer", "primary")}</div>
  </div>`);
}

function taskDialog(id) {
  const item = state.project.items.find(task => task.id === id);
  showDialog(item ? "Править задачу" : "Новая задача", `<form class="stack" data-form="task">
    ${item ? `<input type="hidden" name="id" value="${item.id}">` : ""}
    ${input("title", "Название", { required: true, limit: 160, value: item?.title })}
    ${textarea("details", "Детали", { limit: 3000, value: item?.details })}
    ${select("status", "Статус", Object.entries(statuses), item?.status || "todo")}
    <div class="actions">${submit("Сохранить задачу")}${item ? button("Удалить задачу", "delete-task", "danger", item.id) : ""}</div>
  </form>`);
}

function refreshDialog() {
  const kind = state.project.source.kind;
  const archive = kind === "zip" ? archiveField("Версия ZIP") : "";
  showDialog("Обновить анализ", `<form class="stack" data-form="refresh">
    ${archive}
    ${checkbox("repeat", "Повторить анализ, даже если источник не изменился")}
    <p class="fine">Выбранные материалы будут обработаны моделью ${escapeHtml(state.bootstrap.settings.model)}.</p>
    <div class="actions">${submit("Обновить анализ")}
      ${["github", "local_git", "zip"].includes(kind) ?
        '<button type="submit" class="button" value="snapshot">Сохранить снимок без AI</button>' : ""}
    </div>
  </form>`);
}

function encodeFile(file) {
  if (!file || file.size > 64 * 1024 * 1024) return Promise.reject(new Error("В браузере доступен ZIP до 64 МиБ. Выберите более крупный архив через приложение Windows."));
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(new Error("Не удалось прочитать выбранный архив"));
    reader.readAsDataURL(file);
  });
}

async function readForm(form) {
  const elements = Object.fromEntries(new FormData(form));
  if (elements.archive instanceof File) {
    elements.name = elements.archive.name;
    elements.content = await encodeFile(elements.archive);
    delete elements.archive;
  }
  return elements;
}

async function projectUpdate(action, details, message, tab = state.tab) {
  const updated = await (action === "refresh" ? runAnalysis : post)(action, { project_id: state.project.id, ...details });
  state.project = updated;
  state.tab = tab;
  if (dialog.open) dialog.close();
  await reload();
  render();
  notify(message);
}

function openIfConfigured(work) {
  if (activeProvider().missingKey) {
    notify("Добавьте ключ " + activeProvider().label + " в настройках.", true);
    return;
  }
  work();
}

async function handleClick(buttonElement) {
  const { action, value, nav } = buttonElement.dataset;
  if (nav || action === "navigate") {
    keepDraft();
    navigate(nav || value);
    return;
  }
  switch (action) {
    case "new":
      state.createKind = value || "manual";
      navigate("create");
      break;
    case "create-kind":
      state.createKind = value;
      render();
      break;
    case "training-kind":
      state.trainingKind = value;
      render();
      break;
    case "choose-archive":
      window.chrome?.webview?.postMessage("choose-archive");
      break;
    case "choose-folder":
      window.chrome?.webview?.postMessage("choose-repository");
      break;
    case "audit-page": {
      const audit = state.view === "training" ? state.review?.metadata : state.project?.report?.audit;
      const target = buttonElement.closest("[data-audit-excerpts]");
      if (target && audit) target.innerHTML = auditExcerpts(audit.source_excerpts, Number(value));
      break;
    }
    case "project":
      await run(buttonElement, () => openProject(Number(value)));
      break;
    case "project-tab":
      keepDraft();
      state.tab = value;
      render();
      break;
    case "refresh":
      if (["github", "local_git", "zip"].includes(state.project.source.kind)) refreshDialog();
      else openIfConfigured(refreshDialog);
      break;
    case "edit-project":
      showDialog("Редактировать проект", `<form class="stack" data-form="edit-project">
        ${input("name", "Название", { required: true, value: state.project.name })}
        ${textarea("description", "Описание для анализа", { value: state.project.description || "" })}
        <div class="actions">${submit("Сохранить")}</div>
      </form>`);
      break;
    case "delete-project":
      if (!confirm("Удалить проект, версии, план работ и черновик КП?")) break;
      await run(buttonElement, async () => {
        await post("delete", { kind: "project", id: state.project.id });
        state.project = null;
        state.offerDraft = null;
        await reload();
        render();
        notify("Проект удалён");
      });
      break;
    case "close-dialog":
      dialog.close();
      break;
    case "edit-task":
      taskDialog(Number(value));
      break;
    case "task-status": {
      const [id, status] = value.split(":");
      const item = state.project.items.find(task => task.id === Number(id));
      await run(buttonElement, () => projectUpdate("task", { ...item, status }, "Статус обновлён"));
      break;
    }
    case "delete-task":
      if (confirm("Удалить задачу из плана?")) {
        await run(buttonElement, () => projectUpdate("delete-task", { id: value }, "Задача удалена"));
      }
      break;
    case "add-recommendation":
      await run(buttonElement, () => projectUpdate(
        "task", { recommendation: Number(value), version_ref: state.project.report.version_ref }, "Рекомендация в плане",
      ));
      break;
    case "price": {
      const recommendation = state.project.report.recommendations[Number(value)];
      showDialog("Изменить смету", `<form class="stack" data-form="price" data-index="${Number(value)}">
        <p>${escapeHtml(recommendation.title)}</p>
        <div class="field-grid">
          ${input("cost_min", "От", { value: recommendation.cost_min, type: "number", required: true, extra: "min=0 max=1000000000" })}
          ${input("cost_max", "До", { value: recommendation.cost_max, type: "number", required: true, extra: "min=0 max=1000000000" })}
        </div>
        <div class="actions">${submit("Сохранить смету")}</div>
      </form>`);
      break;
    }
    case "version":
      await run(buttonElement, async () => {
        const report = await post("version", { project_id: state.project.id, revision_id: value });
        showDialog("Сохранённый отчёт", `<div class="stack">
          <h3>${escapeHtml(report.project_name)}</h3><p class="break">${escapeHtml(report.summary)}</p>
          ${featureList(report)}
          <ul class="plain-list">${report.recommendations.map(rec => `<li>
            <strong>${escapeHtml(rec.title)}</strong><p>${escapeHtml(rec.description)}</p>
            <span class="fine">${estimateText(rec, report.currency)}</span>
          </li>`).join("")}</ul>
        </div>`);
      });
      break;
    case "copy-offer":
      await run(buttonElement, async () => {
        const editor = dialog.open ? $("#client-offer-draft textarea", dialog) : $('[data-form="save-offer"] textarea');
        if (!editor.value.trim()) throw new Error("Сначала заполните черновик");
        await navigator.clipboard.writeText(editor.value);
        notify("Текст КП скопирован");
      });
      break;
    case "project-client-text":
    case "training-client-text":
      await run(buttonElement, async () => {
        const training = action === "training-client-text";
        const data = training ? { training_id: state.review.id } : { project_id: state.project.id };
        const result = await post(training ? "training-offer" : "offer", { ...data, indices: [Number(value)] });
        showClientText(result.text);
      });
      break;
    case "training-offer": {
      const options = state.review.ideas.map((idea, index) =>
        idea.description && idea.business_value && idea.success_metric
          ? `<label class="checkbox"><input type="checkbox" name="recommendation" value="${index}" checked> ${escapeHtml(idea.client_title || idea.title)}</label>`
          : "",
      ).join("");
      showDialog("Коммерческое предложение", `<form class="stack" data-form="training-offer">
        ${options}<div class="actions">${submit("Собрать черновик")}</div>
      </form>`);
      break;
    }
    case "review":
      await run(buttonElement, async () => {
        state.review = await request("/api/training?id=" + Number(value));
        render();
        $("#active-review")?.scrollIntoView({ block: "start" });
      });
      break;
    case "retrain":
      await run(buttonElement, async () => {
        state.review = await runAnalysis("retrain", { training_id: state.review.id });
        await reload();
        render();
        $("#active-review")?.scrollIntoView({ block: "start" });
        notify("Создан новый разбор с учётом памяти");
      });
      break;
    case "memory-edit": {
      const item = state.review.feedback.find(f => f.id === Number(value));
      showDialog("Изменить запись памяти", `<form class="stack" data-form="memory-edit" data-id="${item.id}">
        ${textarea("comment", "Объяснение или идея", { value: item.comment, required: !item.rating, limit: 1000 })}
        ${textarea("correction", "Ваш вариант решения", { value: item.correction || "", limit: 1500 })}
        ${select("scope", "Где учитывать", memoryScopes, item.scope)}<div class="actions">${submit("Сохранить")}</div>
      </form>`);
      break;
    }
    case "memory-toggle":
    case "memory-delete":
      if (action === "memory-delete" && !confirm("Удалить эту оценку из памяти?")) break;
      await run(buttonElement, async () => {
        state.review = await post("memory", { training_id: state.review.id, id: Number(value), operation: action === "memory-toggle" ? "toggle" : "delete" });
        await reload();
        render();
        notify("Память обновлена");
      });
      break;
    case "delete-review":
      if (confirm("Удалить разбор и все оценки?")) {
        await run(buttonElement, async () => {
          await post("delete", { kind: "training", id: state.review.id });
          state.review = null;
          await reload();
          render();
          notify("Разбор удалён");
        });
      }
      break;
    case "check":
      await run(buttonElement, async () => {
        const result = await post("check", { service: value });
        const label = $("#connection-" + value);
        if (label) label.textContent = result.login ? "Подключено · " + result.login : result.message;
        if (result.models) {
          state.models[value] = result.models;
          if ($('[name="provider"]')?.value === value) {
            const selected = $('[name="model"]').value;
            const accessible = result.models.some(model => model.id === selected);
            populateModels(value, accessible ? selected : result.models[0].id);
          }
        }
        notify(result.message);
      });
      break;
    case "clear-key":
      if (!confirm("Удалить сохранённый ключ?")) break;
      await run(buttonElement, async () => {
        await post("settings", { ["clear_" + value]: true });
        await reload();
        render();
        notify("Ключ удалён");
      });
      break;
  }
}

async function handleSubmit(form, submitter) {
  const action = form.dataset.form;
  const fields = await readForm(form);
  switch (action) {
    case "analyze":
    case "manual": {
      const project = await (action === "analyze" ? runAnalysis : post)(action, { kind: form.dataset.kind, ...fields, snapshot_only: submitter.value === "snapshot" });
      await reload();
      const snapshot = submitter.value === "snapshot";
      acceptProject(project, action === "manual" ? "plan" : snapshot ? "history" : "report");
      notify(action === "manual" ? "Проект создан" : snapshot ? "Снимок сохранён" : "Анализ готов");
      break;
    }
    case "refresh":
      dialog.close();
      state.comparison = null;
      await projectUpdate(action, {
        ...fields, repeat: form.elements.repeat.checked, snapshot_only: submitter.value === "snapshot",
      }, "Версия сохранена", "history");
      break;
    case "edit-project":
    case "task":
      await projectUpdate(action, fields, action === "task" ? "Задача сохранена" : "Проект обновлён");
      break;
    case "price":
      await projectUpdate(action, { ...fields, index: Number(form.dataset.index), version_ref: state.project.report.version_ref }, "Смета сохранена");
      break;
    case "compare":
    case "git-diff": {
      const result = await post(action, { ...fields, project_id: state.project.id });
      state.comparison = { ...fields, result };
      render();
      $("#comparison").scrollIntoView({ block: "nearest" });
      break;
    }
    case "offer": {
      const indices = new FormData(form).getAll("recommendation").map(Number);
      if ((state.offerDraft || state.project.offer) && !confirm("Заменить текст в редакторе новым предложением?")) break;
      const result = await post(action, { project_id: state.project.id, indices });
      state.offerDraft = result.text;
      state.tab = "offer";
      render();
      break;
    }
    case "training-offer": {
      const indices = new FormData(form).getAll("recommendation").map(Number);
      const result = await post(action, { training_id: state.review.id, indices });
      showClientText(result.text);
      break;
    }
    case "save-offer":
      state.offerDraft = fields.text;
      await projectUpdate(action, fields, "Черновик сохранён");
      state.offerDraft = null;
      break;
    case "train":
      state.review = await runAnalysis(action, { ...fields, kind: form.dataset.kind });
      await reload();
      render();
      $("#active-review")?.scrollIntoView({ block: "start" });
      notify(state.review.metadata?.coverage_mode === "partial" ? "Частичный разбор сохранён" : "Разбор готов. Оцените предложенные решения.");
      break;
    case "feedback":
    case "own-idea": {
      if (action === "feedback" && !fields.rating) throw new Error("Выберите оценку от 1 до 5");
      const index = Number(form.dataset.index ?? -1);
      state.review = await post("feedback", {
        training_id: state.review.id, idea_index: index, comment: fields.comment, scope: fields.scope,
        ...(action === "own-idea" ? { verdict: "own" } : { rating: fields.rating, correction: fields.correction }),
      });
      await reload();
      render();
      const evaluated = new Set(state.review.feedback.map(item => item.idea_index));
      const next = state.review.ideas.findIndex((_, i) => i > index && !evaluated.has(i));
      if (index >= 0 && next >= 0) $("#review-idea-" + next)?.scrollIntoView({ block: "start" });
      notify(action === "own-idea" ? "Идея сохранена в памяти" : "Оценка сохранена в памяти");
      break;
    }
    case "memory-edit":
      state.review = await post("memory", { ...fields, training_id: state.review.id, id: Number(form.dataset.id), operation: "edit" });
      dialog.close();
      await reload();
      render();
      notify("Запись памяти изменена");
      break;
    case "settings":
      fields.thinking = form.elements.thinking.checked;
      fields.use_feedback = form.elements.use_feedback.checked;
      await post(action, fields);
      await reload();
      render();
      notify("Настройки сохранены");
      break;
  }
}

document.addEventListener("click", event => {
  const selected = event.target.closest("button[data-action], button[data-nav]");
  if (selected && state.bootstrap) handleClick(selected).catch(error => notify(error.message, true));
});

document.addEventListener("submit", event => {
  const form = event.target;
  if (!form.dataset.form) return;
  event.preventDefault();
  const submitter = event.submitter || $('button[type="submit"]', form);
  run(submitter, () => handleSubmit(form, submitter));
});

document.addEventListener("input", event => {
  if (event.target.name === "query") {
    state.query = event.target.value;
    $("#project-list").innerHTML = projectListBody();
  }
  if (event.target.closest('[data-form="save-offer"]')) state.offerDraft = event.target.value;
});

document.addEventListener("change", event => {
  if (event.target.name !== "provider") return;
  const provider = event.target.value;
  document.querySelectorAll("[data-provider-panel]").forEach(panel => {
    panel.hidden = panel.dataset.providerPanel !== provider;
  });
  populateModels(provider, state.bootstrap.settings.provider_models[provider]);
});

document.addEventListener("keydown", event => {
  if (event.ctrlKey && event.key.toLowerCase() === "n" && !dialog.open && state.bootstrap) {
    event.preventDefault();
    keepDraft();
    state.createKind = "manual";
    navigate("create");
  }
});

window.chrome?.webview?.addEventListener("message", event => {
  const folder = $('[name="path"]');
  if (typeof event.data !== "string" || !event.data) return;
  if (event.data.startsWith("archive:")) {
    const archive = $('[name="archive_path"]', dialog.open ? dialog : content);
    if (archive) archive.value = event.data.slice(8);
  } else if (folder) {
    folder.value = event.data;
  }
});

const logInterfaceError = () => window.chrome?.webview?.postMessage("interface-error");
window.addEventListener("error", logInterfaceError);
window.addEventListener("unhandledrejection", logInterfaceError);

reload().then(() => {
  render();
  window.chrome?.webview?.postMessage("interface-ready");
  if (state.bootstrap.pending_job) followJob(state.bootstrap.pending_job).catch(error => notify(error.message, true));
}).catch(error => {
  logInterfaceError();
  content.setAttribute("aria-busy", "false");
  content.innerHTML = empty("Не удалось открыть Oberon. " + error.message);
});
