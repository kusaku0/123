"use strict";

(() => {
  const SOURCES = [
    { id: "wildberries", name: "WB", logo: "WB" },
    { id: "ozon", name: "Ozon", logo: "OZ" },
    { id: "aliexpress", name: "AliExpress", logo: "Ali" },
    { id: "avito", name: "Avito", logo: "Av" },
  ];
  const sourceMap = new Map(SOURCES.map(source => [source.id, source]));
  const statusLabels = { ok: "Предложения найдены", empty: "Нет совпадений в выдаче", blocked: "Доступ ограничен", timeout: "Не ответил вовремя", error: "Поиск недоступен", idle: "Готов к поиску", loading: "Ищем предложения…" };
  const $ = id => document.getElementById(id);
  const form = $("search-form");
  const input = $("search-query");
  const submit = $("search-submit");
  const submitLabel = $("submit-label");
  const money = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
  let activeController = null;
  let lastQuery = "";
  let currentData = null;
  let activeSource = "all";
  let refreshNext = false;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function announce(text) { $("live-announcement").textContent = text; }

  function validLink(value, source) {
    if (typeof value !== "string") return null;
    try {
      const url = new URL(value);
      const hosts = { wildberries: ["wildberries.ru"], ozon: ["ozon.ru"], aliexpress: ["aliexpress.ru", "aliexpress.com"], avito: ["avito.ru"] };
      if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) return null;
      if (!(hosts[source] || []).some(host => url.hostname === host || url.hostname.endsWith("." + host))) return null;
      return url.href;
    } catch { return null; }
  }

  function externalLink(url, className, label) {
    const link = node("a", className, label);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  function externalArrow() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("fill", "none");
    svg.setAttribute("aria-hidden", "true");
    svg.classList.add("external-arrow");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M4 4h8v8M12 4 4 12");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.4");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
    return svg;
  }

  function logo(source) {
    return node("span", "market-logo logo-" + source.id, source.logo);
  }

  async function openStoreBrowser(source, button, message) {
    button.disabled = true;
    button.textContent = "Открываем окно…";
    message.textContent = "Подождите, пока откроется окно магазина.";
    message.dataset.state = "pending";
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 45000);
    try {
      const response = await fetch("/api/browser/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source: source.id, query: lastQuery }),
        signal: controller.signal,
      });
      let data;
      try { data = await response.json(); } catch { throw new Error("Сервер вернул неожиданный ответ. Попробуйте открыть окно ещё раз."); }
      if (!response.ok) throw new Error(data && typeof data.detail === "string" ? data.detail : "Не удалось открыть окно магазина. Попробуйте ещё раз.");
      if (!data || data.status !== "opened") throw new Error("Не удалось подтвердить открытие окна. Попробуйте ещё раз.");
      refreshNext = true;
      message.dataset.state = "success";
      message.textContent = "Окно магазина открыто на этом компьютере. После проверки нажмите «Найти дешевле» ещё раз.";
      announce(`${source.name}: окно магазина открыто. После проверки повторите поиск.`);
    } catch (error) {
      message.dataset.state = "error";
      message.textContent = controller.signal.aborted ? "Сервер не ответил вовремя. Проверьте, появилось ли окно магазина, или повторите попытку." : error instanceof TypeError ? "Нет соединения с сервером. Проверьте подключение и попробуйте ещё раз." : error.message || "Не удалось открыть окно магазина. Попробуйте ещё раз.";
    } finally {
      window.clearTimeout(timeout);
      button.disabled = false;
      button.textContent = "Открыть окно магазина";
    }
  }

  function renderSources(rows = [], state = "idle") {
    const bySource = new Map(rows.map(row => [row.source, row]));
    const container = $("source-list");
    container.replaceChildren();
    for (const source of SOURCES) {
      const row = bySource.get(source.id);
      const status = row && Object.hasOwn(statusLabels, row.status) ? row.status : state;
      const card = node("article", "source-card");
      card.dataset.status = status;
      const top = node("div", "source-top");
      top.append(logo(source), node("span", "source-name", source.name));
      const statusLine = node("div", "source-status");
      statusLine.append(node("span", "status-dot"), node("span", "", statusLabels[status]));
      card.append(top, statusLine);
      if (row && row.message) card.append(node("p", "source-message", String(row.message)));
      const url = row && validLink(row.search_url, source.id);
      if (url) {
        const link = externalLink(url, "source-link", "Открыть поиск");
        link.append(externalArrow());
        card.append(link);
      }
      if (row && row.can_open_browser === true && ["blocked", "error", "timeout"].includes(status)) {
        const controls = node("div", "source-browser-controls");
        const button = node("button", "source-browser-button", "Открыть окно магазина");
        button.type = "button";
        button.dataset.openBrowser = source.id;
        const message = node("p", "source-browser-message", "Пройдите проверку, если она появится, затем повторите поиск.");
        message.id = "browser-message-" + source.id;
        message.setAttribute("role", "status");
        message.setAttribute("aria-live", "polite");
        message.setAttribute("aria-atomic", "true");
        button.setAttribute("aria-describedby", message.id);
        button.addEventListener("click", () => openStoreBrowser(source, button, message));
        controls.append(button, message);
        card.append(controls);
      }
      container.append(card);
    }
  }

  function pluralOffers(count) {
    const last = count % 10;
    const lastTwo = count % 100;
    const ending = lastTwo >= 11 && lastTwo <= 14 ? "предложений" : last === 1 ? "предложение" : last >= 2 && last <= 4 ? "предложения" : "предложений";
    return `${count} ${ending}`;
  }

  function validProducts(data) {
    if (!Array.isArray(data.products)) return [];
    return data.products.filter(product => product && sourceMap.has(product.source) && typeof product.title === "string" && product.currency === "RUB" && Number.isFinite(Number(product.price)) && Number(product.price) > 0 && validLink(product.url, product.source));
  }

  function renderFilters() {
    const container = $("result-filters");
    const products = validProducts(currentData);
    container.replaceChildren();
    for (const source of [{ id: "all", name: "Все площадки" }, ...SOURCES]) {
      const count = source.id === "all" ? products.length : products.filter(product => product.source === source.id).length;
      const button = node("button", "filter-button", source.name);
      button.type = "button";
      button.dataset.source = source.id;
      button.setAttribute("aria-pressed", String(activeSource === source.id));
      button.append(node("span", "filter-count", String(count)));
      button.addEventListener("click", () => {
        activeSource = source.id;
        renderFilters();
        renderProducts();
      });
      container.append(button);
    }
  }

  function renderProducts() {
    const allProducts = validProducts(currentData);
    const products = allProducts.filter(product => activeSource === "all" || product.source === activeSource);
    const direction = $("sort-order").value === "desc" ? -1 : 1;
    products.sort((left, right) => (Number(left.price) - Number(right.price)) * direction);
    const cheapest = Math.min(...allProducts.map(product => Number(product.price)));
    const container = $("product-list");
    container.replaceChildren();
    for (const product of products) {
      const source = sourceMap.get(product.source);
      const article = node("article", "product-card");
      const top = node("div", "product-top");
      const sourceLabel = node("div", "product-source");
      sourceLabel.append(logo(source), node("span", "", source.name));
      top.append(sourceLabel);
      if (Number(product.price) === cheapest) top.append(node("span", "best-label", "Дешевле среди найденных"));
      const price = node("div", "product-price", money.format(Number(product.price)) + " ");
      price.append(node("span", "product-currency", "₽"));
      const link = externalLink(validLink(product.url, product.source), "product-link", "Смотреть на " + source.name);
      link.append(externalArrow());
      article.append(top, node("h3", "product-title", product.title), price, node("span", "product-meta", product.source === "avito" ? "Без доставки · Проверьте состояние" : "Без доставки · Условия цены на сайте"), link);
      container.append(article);
    }
    const empty = $("empty-results");
    empty.hidden = products.length > 0;
    if (!products.length) {
      const rows = Array.isArray(currentData.sources) ? currentData.sources : [];
      const selected = activeSource === "all" ? rows : rows.filter(row => row.source === activeSource);
      const restricted = selected.some(row => ["blocked", "error", "timeout"].includes(row.status));
      $("empty-title").textContent = restricted ? "Не удалось получить подтверждённые цены" : "Нет совпадений в полученной выдаче";
      $("empty-message").textContent = restricted ? "Площадка могла ограничить доступ или не ответить. Это не означает, что товара нет. Посмотрите статусы выше или откройте поиск на сайте." : "Попробуйте убрать лишние характеристики или изменить название. Мы проверяем доступную страницу, а не весь каталог.";
    }
  }

  function showResults(data) {
    currentData = data;
    activeSource = "all";
    $("sort-order").value = "asc";
    const rows = Array.isArray(data.sources) ? data.sources : [];
    renderSources(rows);
    const city = typeof data.city === "string" ? data.city : "Липецк";
    $("header-city").textContent = city;
    $("results-title").textContent = data.query || lastQuery;
    const count = validProducts(data).length;
    const available = rows.filter(row => row.status === "ok").length;
    $("results-summary").textContent = `${pluralOffers(count)} · ${city} · Цены без доставки${data.cached ? " · Из кратковременного кэша" : ""}`;
    const date = new Date(data.checked_at);
    $("check-time").textContent = Number.isNaN(date.getTime()) ? "Поиск завершён" : "Проверено в " + new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Moscow" }).format(date) + " МСК";
    const failures = rows.filter(row => ["blocked", "timeout", "error"].includes(row.status));
    $("partial-notice").hidden = !failures.length;
    $("partial-notice").textContent = available ? "Результаты неполные: некоторые площадки недоступны. Найденные предложения остальных показаны ниже." : "Часть площадок недоступна. Проверьте статусы и ссылки на ручной поиск — блокировка не означает отсутствие товара.";
    $("results-section").hidden = false;
    renderFilters();
    renderProducts();
    announce(`Поиск завершён: ${pluralOffers(count)}. ${failures.length ? "Некоторые площадки недоступны." : ""}`);
  }

  function setBusy(busy) {
    submit.disabled = busy;
    input.disabled = busy;
    document.querySelectorAll("[data-query]").forEach(button => { button.disabled = busy; });
    submitLabel.textContent = busy ? "Ищем…" : "Найти дешевле";
    form.setAttribute("aria-busy", String(busy));
    $("loading-state").hidden = !busy;
  }

  async function performSearch(event) {
    if (event) event.preventDefault();
    if (activeController) return;
    const query = input.value.trim().replace(/\s+/g, " ");
    $("form-error").hidden = true;
    input.removeAttribute("aria-invalid");
    if (query.length < 2 || query.length > 160 || !/[\p{L}\p{N}]/u.test(query)) {
      $("form-error").textContent = "Введите название товара: от 2 до 160 символов.";
      $("form-error").hidden = false;
      input.setAttribute("aria-invalid", "true");
      input.focus();
      return;
    }
    lastQuery = query;
    const controller = new AbortController();
    activeController = controller;
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, 100000);
    $("results-section").hidden = true;
    $("error-state").hidden = true;
    renderSources([], "loading");
    $("check-time").textContent = "Обновляем предложения…";
    setBusy(true);
    announce("Поиск начался. Ждём ответы площадок.");
    try {
      const payload = refreshNext ? { query, refresh: true } : { query };
      const response = await fetch("/api/search", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: controller.signal });
      let data;
      try { data = await response.json(); } catch { throw new Error("Сервер вернул неожиданный ответ. Попробуйте позже."); }
      if (!response.ok) {
        const fallback = response.status === 429 ? "Слишком много запросов. Подождите немного и повторите поиск." : response.status === 503 ? "Сейчас выполняется несколько поисков. Попробуйте через минуту." : response.status === 422 ? "Проверьте название товара: от 2 до 160 символов." : "Поиск временно недоступен. Попробуйте позже.";
        throw new Error(typeof data.detail === "string" ? data.detail : fallback);
      }
      if (!data || !Array.isArray(data.products) || !Array.isArray(data.sources)) throw new Error("Не удалось прочитать результаты. Попробуйте позже.");
      refreshNext = false;
      showResults(data);
    } catch (error) {
      renderSources();
      $("check-time").textContent = "Поиск не завершён";
      $("error-state").hidden = false;
      const aborted = controller.signal.aborted;
      $("error-title").textContent = aborted && !timedOut ? "Поиск отменён" : "Не удалось выполнить поиск";
      $("error-message").textContent = aborted ? timedOut ? "Сервер не ответил вовремя. Попробуйте повторить поиск чуть позже." : "Можно изменить название товара или попробовать ещё раз." : error instanceof TypeError ? "Нет соединения с сервером. Проверьте подключение и попробуйте ещё раз." : error.message || "Попробуйте немного позже.";
      announce($("error-title").textContent + ". " + $("error-message").textContent);
    } finally {
      window.clearTimeout(timeout);
      activeController = null;
      setBusy(false);
    }
  }

  form.addEventListener("submit", performSearch);
  $("cancel-search").addEventListener("click", () => { if (activeController) activeController.abort(); });
  $("retry-search").addEventListener("click", () => { input.value = lastQuery; performSearch(); });
  $("sort-order").addEventListener("change", () => { if (currentData) renderProducts(); });
  document.querySelectorAll("[data-query]").forEach(button => button.addEventListener("click", () => { input.value = button.dataset.query; input.focus(); }));
  renderSources();
})();
