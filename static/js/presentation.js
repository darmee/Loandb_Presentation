/* DashMFB Loan Dashboard - the presentation deck.
 *
 * One page, one screen per tab, in the shape of the original deck: an
 * overview of all requests, a monthly overview, one page per product, then
 * daily activity, applications vs disbursements, the journey of a single
 * request, a period comparison and, last, the top insights.
 *
 * /api/dashboard/ returns everything the deck pages need in one call; the
 * page draws only the tab on screen (ECharts cannot size a hidden chart).
 * Every clickable chart element opens the drawer with the requests behind
 * it (from /api/drill/ or /api/day/), and every row opens that request's
 * journey.
 *
 * Server data is only ever inserted with textContent, or through esc()
 * inside chart tooltips, never as raw HTML.
 */
(function () {
  "use strict";

  const CFG = JSON.parse(document.getElementById("app-config").textContent);
  const PRODUCT_CODES = CFG.products.map((p) => p.code);
  const PRODUCT_LABELS = Object.fromEntries(CFG.products.map((p) => [p.code, p.label]));
  const TABS = ["overview", "monthly", ...PRODUCT_CODES.map((c) => `product-${c}`), "daily", "flow", "compare", "insights"];
  const PLAY_TABS = ["overview", "monthly", ...PRODUCT_CODES.map((c) => `product-${c}`), "daily", "flow", "insights"];
  const PLAY_INTERVAL = 15000;
  const REFRESH_INTERVAL = 10 * 60 * 1000;
  const DAILY_VISIBLE = 14;   // days shown at once on daily charts, so bars stay wide

  const state = {
    tab: "overview",
    preset: "all",
    start: null,
    end: null,
    data: null,
    rendered: new Set(),
    dailyMode: "bar",
    dailyProduct: "all",
    compareMetric: "submitted",
    compare: null,
    monthlyMode: "count",
    distMode: "bands",
    playing: false,
  };
  const charts = {};

  // --- helpers ---------------------------------------------------------------

  const $ = (id) => document.getElementById(id);

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "style") el.setAttribute("style", v);
        else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
        else if (k === "text") el.textContent = v;
        else el.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const child of children.flat()) {
      if (child == null || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  const nf = new Intl.NumberFormat("en-NG");
  const fmt = (n) => (n == null ? "–" : nf.format(Math.round(n)));
  const pct = (n) => (n == null ? "–" : `${Number(n).toFixed(1).replace(/\.0$/, "")}%`);
  function days(n) {
    if (n == null) return "–";
    if (n < 1) { const hrs = Math.round(n * 24); return hrs < 1 ? "< 1 h" : `${hrs} h`; }
    return `${Number(n).toFixed(1).replace(/\.0$/, "")} d`;
  }
  function compact(n) {
    if (n == null) return "–";
    const abs = Math.abs(n);
    for (const [limit, suffix] of [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
      if (abs >= limit) return `${(n / limit).toFixed(1).replace(/\.0$/, "")}${suffix}`;
    }
    return nf.format(Math.round(n));
  }
  const money = (n) => (n == null ? "–" : `₦${compact(n)}`);
  const moneyFull = (n) => (n == null ? "–" : `₦${nf.format(Math.round(n))}`);

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  function parseDay(iso) { const [y, m, d] = iso.slice(0, 10).split("-").map(Number); return new Date(y, m - 1, d); }
  function isoDay(date) { const p = (n) => String(n).padStart(2, "0"); return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}`; }
  function addDays(iso, n) { const d = parseDay(iso); d.setDate(d.getDate() + n); return isoDay(d); }
  const dayCount = (a, b) => Math.round((parseDay(b) - parseDay(a)) / 86400000) + 1;
  const longDate = (iso) => { const d = parseDay(iso); return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`; };
  const shortDate = (iso) => { const d = parseDay(iso); return `${d.getDate()} ${MONTHS[d.getMonth()]}`; };
  const weekdayDate = (iso) => { const d = parseDay(iso); return `${WEEKDAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`; };
  const monthLabel = (key) => { const [y, m] = key.split("-").map(Number); return `${MONTHS[m - 1]} ${String(y).slice(2)}`; };
  const monthLong = (key) => { const [y, m] = key.split("-").map(Number); return `${MONTHS[m - 1]} ${y}`; };
  function dateTime(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function toast(message) {
    const el = $("toast");
    el.textContent = message;
    el.classList.add("on");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.classList.remove("on"), 2400);
  }

  // --- theme & palette ---------------------------------------------------------
  // Categorical slots in the validated order, stepped separately for the dark
  // and light surfaces. A colour belongs to one thing everywhere on the page.

  const PALETTES = {
    dark: {
      submitted: "#3987e5", reviewed: "#199e70", credit_approved: "#9085e9", control_approved: "#c98500",
      disbursed: "#0ca30c", rejected: "#e66767", correction: "#d95926",
      products: ["#3987e5", "#199e70", "#9085e9"],
      outcome: { PENDING: "#8d8996", IN_PROGRESS: "#3987e5", DISBURSED: "#0ca30c", REJECTED: "#e66767" },
      dash: "#9085e9", floauto: "#d95926", equal: "#8d8996",
      bar: "#3987e5", bar2: "#199e70", a: "#3987e5", b: "#d95926",
      seq: ["#184f95", "#256abf", "#3987e5", "#5598e7", "#86b6ef", "#b7d3f6"],
      ink: "#f4f2f7", ink2: "#c3c0cc", muted: "#8d8996", grid: "#2a2730", axis: "#3d3946",
      surface: "#17151c", tooltip: "#24212b", calEmpty: "#211e27",
    },
    light: {
      submitted: "#2a78d6", reviewed: "#1baf7a", credit_approved: "#4a3aa7", control_approved: "#eda100",
      disbursed: "#008300", rejected: "#e34948", correction: "#eb6834",
      products: ["#2a78d6", "#1baf7a", "#4a3aa7"],
      outcome: { PENDING: "#94A3B8", IN_PROGRESS: "#2a78d6", DISBURSED: "#008300", REJECTED: "#e34948" },
      dash: "#4a3aa7", floauto: "#eb6834", equal: "#94A3B8",
      bar: "#2a78d6", bar2: "#1baf7a", a: "#2a78d6", b: "#eb6834",
      seq: ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"],
      ink: "#18161c", ink2: "#4d4955", muted: "#77737f", grid: "#ebe8ef", axis: "#cfcad6",
      surface: "#ffffff", tooltip: "#ffffff", calEmpty: "#f1eef4",
    },
  };
  const theme = () => (document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");
  const P = () => PALETTES[theme()];
  const productColour = (code) => P().products[Math.max(0, PRODUCT_CODES.indexOf(code)) % 3];
  const DAILY_KEYS = ["submitted", "reviewed", "control_approved", "disbursed", "rejected"];
  const DAILY_LABELS = { submitted: "Submitted", reviewed: "Reviewed", control_approved: "Approved", disbursed: "Disbursed", rejected: "Rejected" };

  // --- charts ------------------------------------------------------------------

  function baseOption(option) {
    const p = P();
    const tooltip = Object.assign({
      backgroundColor: p.tooltip, borderColor: p.axis, borderWidth: 1, padding: [8, 11],
      textStyle: { color: p.ink, fontSize: 13 }, confine: true,
      extraCssText: "border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.3);",
    }, option.tooltip || {});
    const legend = option.legend ? Object.assign({
      top: 0, left: 0, icon: "roundRect", itemWidth: 11, itemHeight: 11, itemGap: 16,
      textStyle: { color: p.ink2, fontSize: 12.5 }, inactiveColor: p.axis,
    }, option.legend) : undefined;
    return Object.assign({
      backgroundColor: "transparent",
      animationDuration: 450,
      textStyle: { fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif", color: p.ink2 },
    }, option, { tooltip, legend });
  }
  function catAxis(data, extra) {
    const p = P();
    return Object.assign({
      type: "category", data,
      axisLine: { lineStyle: { color: p.axis } }, axisTick: { show: false },
      axisLabel: { color: p.ink2, fontSize: 12, hideOverlap: true },
    }, extra || {});
  }
  function valAxis(extra) {
    const p = P();
    return Object.assign({
      type: "value", splitLine: { lineStyle: { color: p.grid } },
      axisLabel: { color: p.muted, fontSize: 11 }, axisLine: { show: false },
    }, extra || {});
  }
  const grid = (extra) => Object.assign({ left: 6, right: 14, top: 26, bottom: 4, containLabel: true }, extra || {});

  // Wide, clearly visible bars: the category gap is a share of each slot.
  function bar(name, data, color, extra) {
    return Object.assign({
      name, type: "bar", data, barCategoryGap: "28%", barGap: "12%", barMaxWidth: 90,
      itemStyle: { color, borderRadius: [5, 5, 0, 0] },
      emphasis: { focus: "none" },
    }, extra || {});
  }
  function hbar(name, data, color, extra) {
    return bar(name, data, color, Object.assign({ itemStyle: { color, borderRadius: [0, 5, 5, 0] }, barCategoryGap: "26%", barMaxWidth: 34 }, extra || {}));
  }
  function line(name, data, color, extra) {
    return Object.assign({
      name, type: "line", data, smooth: 0.25, symbol: "circle", symbolSize: 7, showSymbol: data.length <= 45,
      lineStyle: { width: 2.5, color }, itemStyle: { color }, emphasis: { focus: "none" },
    }, extra || {});
  }

  /* Draw (or redraw) one chart. `onClick` gets the clicked element's params;
     `onDay` gets the x-axis index under the pointer, so a click anywhere in
     a day's column - not only on its bar - opens that day. */
  function draw(id, option, handlers) {
    const el = $(id);
    if (!el || !window.echarts) return null;
    let chart = charts[id];
    if (!chart || chart.isDisposed()) {
      chart = echarts.init(el, null, { renderer: "canvas" });
      charts[id] = chart;
      if (window.ResizeObserver) {
        const ro = new ResizeObserver(() => { if (!chart.isDisposed() && el.offsetParent !== null) chart.resize(); });
        ro.observe(el);
        chart.__ro = ro;
      }
    }
    chart.setOption(baseOption(option), true);
    chart.off("click");
    // Remove only our own zrender handlers: zr.off("click") with no handler
    // would also strip ECharts' internal listeners and break every click.
    const zr = chart.getZr();
    if (chart.__dayClick) zr.off("click", chart.__dayClick);
    if (chart.__dayMove) zr.off("mousemove", chart.__dayMove);
    chart.__dayClick = chart.__dayMove = null;
    handlers = handlers || {};
    if (handlers.onClick) chart.on("click", handlers.onClick);
    if (handlers.onDay) {
      chart.__dayClick = (ev) => {
        const point = [ev.offsetX, ev.offsetY];
        if (!chart.containPixel("grid", point)) return;
        const index = chart.convertFromPixel({ gridIndex: 0 }, point)[0];
        if (index != null && index >= 0) handlers.onDay(Math.round(index));
      };
      chart.__dayMove = (ev) => zr.setCursorStyle(chart.containPixel("grid", [ev.offsetX, ev.offsetY]) ? "pointer" : "default");
      zr.on("click", chart.__dayClick);
      zr.on("mousemove", chart.__dayMove);
    }
    return chart;
  }
  function disposeAll() {
    for (const [id, chart] of Object.entries(charts)) {
      if (chart.__ro) chart.__ro.disconnect();
      chart.dispose();
      delete charts[id];
    }
  }
  function resizeVisible() {
    for (const chart of Object.values(charts)) {
      const el = chart.getDom();
      if (el && el.offsetParent !== null) chart.resize();
    }
  }

  function tipRows(title, items, footer) {
    let html = `<div style="font-weight:700;margin-bottom:4px">${esc(title)}</div>`;
    for (const it of items) {
      if (!it) continue;
      html += `<div style="display:flex;align-items:center;gap:10px;justify-content:space-between;min-width:180px">` +
        `<span>${it.color ? `<span style="display:inline-block;width:9px;height:9px;border-radius:3px;background:${it.color};margin-right:6px"></span>` : ""}${esc(it.label)}</span>` +
        `<b style="font-variant-numeric:tabular-nums">${esc(it.value)}</b></div>`;
    }
    if (footer) html += `<div style="margin-top:6px;color:${P().muted};font-size:11px">${esc(footer)}</div>`;
    return html;
  }

  // --- networking ------------------------------------------------------------

  let pending = 0;
  async function api(path, params) {
    const url = new URL(path, window.location.origin);
    for (const [k, v] of Object.entries(params || {})) if (v != null && v !== "") url.searchParams.set(k, v);
    pending += 1;
    $("loading").classList.add("on");
    try {
      const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (response.status === 401) {
        window.location.href = `/login/?next=${encodeURIComponent(window.location.pathname)}`;
        throw new Error("Signed out");
      }
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.error || `The server answered ${response.status}.`);
      return body;
    } finally {
      pending -= 1;
      if (!pending) $("loading").classList.remove("on");
    }
  }

  /* The window the deck pages count, as API parameters. */
  function windowParams() {
    return state.preset === "all" ? { period: "all" } : { period: "range", start: state.start, end: state.end };
  }

  function showError(message) {
    const box = $("error");
    box.hidden = !message;
    box.textContent = message || "";
  }
  function setNotice(text, actionLabel, action) {
    const box = $("notice");
    if (!text) { box.hidden = true; box.replaceChildren(); return; }
    box.hidden = false;
    box.replaceChildren(h("span", { text }), actionLabel ? h("button", { type: "button", class: "btn", text: actionLabel, onclick: action }) : null);
  }

  // --- url state -------------------------------------------------------------

  function readHash() {
    const params = new URLSearchParams(window.location.hash.slice(1));
    if (TABS.includes(params.get("tab"))) state.tab = params.get("tab");
    const valid = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s || "");
    if (valid(params.get("start")) && valid(params.get("end"))) {
      state.start = params.get("start");
      state.end = params.get("end");
      state.preset = null;
    }
  }
  function writeHash() {
    const params = new URLSearchParams({ tab: state.tab });
    if (state.preset !== "all") { params.set("start", state.start); params.set("end", state.end); }
    history.replaceState(null, "", `#${params}`);
  }

  // --- period ----------------------------------------------------------------

  function applyPreset(preset) {
    state.preset = preset;
    state.end = CFG.today;
    if (preset === "all") state.start = null;
    else if (preset === "mtd") state.start = `${CFG.today.slice(0, 8)}01`;
    else state.start = addDays(CFG.today, -(Number(preset) - 1));
  }

  function periodText() {
    if (state.preset === "all") return "all time";
    if (!state.start) return "";
    return `${longDate(state.start)} – ${longDate(state.end)}`;
  }

  function syncFilterControls() {
    if (state.data && state.preset === "all") {
      $("start").value = state.data.window.start;
      $("end").value = state.data.window.end;
    } else {
      $("start").value = state.start || "";
      $("end").value = state.end || "";
    }
    $("start").max = CFG.today;
    $("end").max = CFG.today;
    document.querySelectorAll("#presets button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.preset === state.preset)));
  }

  function bindFilters() {
    document.querySelectorAll("#presets button").forEach((b) => b.addEventListener("click", () => {
      applyPreset(b.dataset.preset);
      reload();
    }));
    for (const id of ["start", "end"]) {
      $(id).addEventListener("change", () => {
        const s = $("start").value, e = $("end").value;
        if (!s || !e) return;
        state.start = s <= e ? s : e;
        state.end = s <= e ? e : s;
        if (dayCount(state.start, state.end) > CFG.maxDays) {
          state.start = addDays(state.end, -(CFG.maxDays - 1));
          toast("Custom ranges are capped at two years; use All time for everything");
        }
        state.preset = null;
        reload();
      });
    }
    document.querySelectorAll(".product-select").forEach((sel) => sel.addEventListener("change", () => {
      state.dailyProduct = sel.value;
      document.querySelectorAll(".product-select").forEach((other) => { other.value = sel.value; });
      state.rendered.delete("daily");
      state.rendered.delete("flow");
      renderTab(state.tab);
    }));
  }

  // --- tabs ------------------------------------------------------------------

  function selectTab(tab, opts) {
    if (!TABS.includes(tab)) tab = "overview";
    state.tab = tab;
    document.querySelectorAll(".tab").forEach((b) => {
      const on = b.dataset.tab === tab;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
      if (on && !(opts && opts.quiet)) b.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("is-active", p.id === `panel-${tab}`));
    writeHash();
    requestAnimationFrame(() => { renderTab(tab); resizeVisible(); });
  }

  function renderTab(tab) {
    if (tab === "compare") {
      if (!state.rendered.has("compare")) { state.rendered.add("compare"); setCompare("previous"); }
      else if (state.compare) renderCompare();
      return;
    }
    if (!state.data || state.rendered.has(tab)) return;
    try {
      if (tab === "overview") renderOverview(state.data);
      else if (tab === "monthly") renderMonthly(state.data);
      else if (tab.startsWith("product-")) renderProduct(state.data, tab.slice(8));
      else if (tab === "daily") renderDaily(state.data);
      else if (tab === "flow") renderFlow(state.data);
      else if (tab === "insights") renderInsights(state.data);
      state.rendered.add(tab);
    } catch (err) {
      console.error(err);
      showError(`Could not draw this page: ${err.message}`);
    }
  }

  // --- loading data ----------------------------------------------------------

  let reloadSeq = 0;
  async function reload(silent) {
    syncFilterControls();
    writeHash();
    // Filters can change faster than the server answers; only the newest
    // request may draw, or a slow earlier answer would overwrite it.
    const seq = ++reloadSeq;
    try {
      const data = await api("/api/dashboard/", windowParams());
      if (seq !== reloadSeq) return;
      state.data = data;
      showError("");
      checkData(data);
      syncFilterControls();
      for (const t of TABS) if (t !== "compare") state.rendered.delete(t);
      renderTab(state.tab);
      requestAnimationFrame(resizeVisible);
    } catch (err) {
      if (!silent && seq === reloadSeq) showError(err.message);
    }
  }

  function checkData(data) {
    const span = data.data || {};
    const dbName = (data.database && data.database.name) || "the loan database";
    if (!span.total) {
      setNotice(`Connected to "${dbName}", but it holds no loan requests. ` +
        (data.database && data.database.sample ? "Fill it with: python manage.py seed_sample_db" : "Check the database settings in .env."));
    } else if (!data.scopes.all.tiles.applications && state.preset !== "all") {
      setNotice(`No requests were submitted between ${longDate(data.window.start)} and ${longDate(data.window.end)}. "${dbName}" has ${fmt(span.total)} requests from ${longDate(span.first)} to ${longDate(span.last)}.`,
        "Show all time", () => { applyPreset("all"); reload(); });
    } else {
      setNotice("");
    }
  }

  // --- drawer ----------------------------------------------------------------

  function openDrawer(title, sub) {
    drawerBack = null;
    $("drawer-back").hidden = true;
    $("drawer-title").textContent = title;
    $("drawer-sub").textContent = sub || "";
    const body = $("drawer-body");
    body.replaceChildren(h("div", { class: "empty", text: "Loading…" }));
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
    $("scrim").classList.add("open");
    pausePlay();
    setTimeout(() => $("drawer-close").focus(), 50);
    return body;
  }
  function closeDrawer() {
    drawerBack = null;
    $("drawer-back").hidden = true;
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
    $("scrim").classList.remove("open");
    for (const id of ["ch-drawer-products", "ch-drawer-hours"]) {
      if (charts[id]) { if (charts[id].__ro) charts[id].__ro.disconnect(); charts[id].dispose(); delete charts[id]; }
    }
  }

  /* What "why" means for a row: the rejection reason, the correction message,
     or the step it is waiting at. */
  function reasonKey(r) {
    if (r.status === "REJECTED") return r.reason || "";
    if (r.status === "CORRECTION_REQUESTED") return `Correction: ${r.reason || "no message"}`;
    if (r.status === "DISBURSED") return "Disbursed";
    const m = /^Waiting at (.+?) for/.exec(r.detail || "");
    return m ? `Waiting at ${m[1]}` : (r.detail || "");
  }

  /* A filterable list of requests; rows open the journey. With `reasons`, a
     clickable summary of why (rejection reason / correction message / where
     it is waiting) sits on top - the "why was it rejected?" view. */
  function requestList(rows, opts) {
    opts = opts || {};
    const wrap = h("div");
    const search = h("input", { class: "input", type: "search", placeholder: "Filter by reference, name, reason…", "aria-label": "Filter list" });
    let eventFilter = null;
    let reasonFilter = null;
    const controls = h("div", { class: "filter-row" }, search);
    if (opts.events) {
      eventFilter = h("select", { class: "input", "aria-label": "Event" },
        h("option", { value: "", text: "All events" }),
        CFG.events.map((e) => h("option", { value: e.key, text: e.label })));
      controls.append(eventFilter);
    }
    const count = h("span", { class: "muted" });
    controls.append(count);

    let summary = null;
    if (opts.reasons) {
      const groups = new Map();
      for (const r of rows) {
        const key = reasonKey(r);
        groups.set(key, (groups.get(key) || 0) + 1);
      }
      const ranked = [...groups.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12);
      if (ranked.length > 1 || (ranked.length === 1 && ranked[0][0] !== "")) {
        const max = ranked[0][1];
        summary = h("div", { class: "reasons" }, h("h3", { text: opts.reasonsTitle || "Breakdown" }),
          ranked.map(([key, n]) => h("button", {
            type: "button", "aria-pressed": "false", title: "Show only these",
            onclick: (e) => {
              const btn = e.currentTarget;
              const on = btn.getAttribute("aria-pressed") !== "true";
              summary.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false"));
              btn.setAttribute("aria-pressed", String(on));
              reasonFilter = on ? key : null;
              paint();
            },
          }, h("span", { text: key || "(not recorded)" }),
            h("span", { class: "track" }, h("span", { class: "fill", style: `display:block;width:${(n * 100) / max}%;background:${opts.colour || P().rejected}` })),
            h("b", { text: fmt(n) }))));
      }
    }

    const table = h("table", { class: "data" });
    const headers = [opts.events ? "Event" : null, opts.events ? "Time" : null, "Reference", "Applicant", "Product", "Amount", "Status", opts.events ? null : "Submitted", opts.detailLabel || "Details"].filter(Boolean);
    table.append(h("thead", null, h("tr", null, headers.map((t) => h("th", { class: t === "Amount" ? "n" : null, text: t })))));
    const tbody = h("tbody");
    table.append(tbody);

    function paint() {
      const q = search.value.trim().toLowerCase();
      const ev = eventFilter ? eventFilter.value : "";
      const shown = rows.filter((r) => (!ev || r.event === ev) &&
        (reasonFilter == null || reasonKey(r) === reasonFilter) &&
        (!q || `${r.reference} ${r.name} ${r.officer || ""} ${r.product_label} ${r.detail || ""}`.toLowerCase().includes(q)));
      tbody.replaceChildren(...shown.slice(0, 400).map((r) => {
        const cells = [];
        if (opts.events) {
          cells.push(h("td", null, h("span", { class: "ev" }, h("i", { style: `background:${P()[r.event]}` }), r.event_label)));
          cells.push(h("td", { class: "num", text: r.time }));
        }
        cells.push(h("td", { style: "white-space:nowrap" }, h("b", { text: r.reference })));
        cells.push(h("td", { text: r.name }));
        cells.push(h("td", { text: r.product_label }));
        cells.push(h("td", { class: "n", text: moneyFull(r.amount) }));
        cells.push(h("td", null, h("span", { class: `pill ${r.status}`, text: r.status_label })));
        if (!opts.events) cells.push(h("td", { class: "num", style: "white-space:nowrap", text: r.submitted ? shortDate(r.submitted) : "–" }));
        cells.push(h("td", { class: "muted", text: r.detail || "" }));
        return h("tr", { class: "click", tabindex: "0", title: "Open this request's journey",
          onclick: () => openJourney(r.product, r.id),
          onkeydown: (e) => { if (e.key === "Enter") openJourney(r.product, r.id); } }, cells);
      }));
      count.textContent = `${fmt(shown.length)} of ${fmt(rows.length)}${shown.length > 400 ? " · first 400 shown" : ""}`;
    }
    search.addEventListener("input", paint);
    if (eventFilter) eventFilter.addEventListener("change", paint);
    paint();
    wrap.append(summary || "", controls, rows.length ? h("div", { class: "table-wrap" }, table) : h("div", { class: "empty", text: "No requests." }));
    return wrap;
  }

  async function openDrill(kind, key, opts) {
    opts = opts || {};
    const body = openDrawer("Loading…", "");
    try {
      const params = Object.assign(windowParams(), { kind, key, product: opts.product && opts.product !== "all" ? opts.product : "" });
      const d = await api("/api/drill/", params);
      $("drawer-title").textContent = opts.title || d.title;
      const scope = opts.product && opts.product !== "all" ? PRODUCT_LABELS[opts.product] : "All products";
      $("drawer-sub").textContent = `${fmt(d.count)} request${d.count === 1 ? "" : "s"} · ${scope} · ${periodText()}. Click one to see its journey.`;
      const rejected = key === "REJECTED";
      body.replaceChildren(requestList(d.rows, {
        reasons: opts.reasons !== false,
        reasonsTitle: rejected ? "Why they were rejected" : key === "IN_PROGRESS" || key === "PENDING" ? "Where they are waiting" : "Breakdown",
        colour: opts.colour,
        detailLabel: rejected ? "Reason" : "Details",
      }));
    } catch (err) {
      body.replaceChildren(h("div", { class: "error-box", text: err.message }));
    }
  }

  async function openDay(date, product) {
    const body = openDrawer(weekdayDate(date), "Loading the day…");
    try {
      const d = await api("/api/day/", { date, product: product && product !== "all" ? product : "" });
      const p = P();
      $("drawer-sub").textContent = `Everything that happened on this day${product && product !== "all" ? ` · ${PRODUCT_LABELS[product]}` : ""}. Click a request to see its journey.`;
      const chips = h("div", { class: "chips" },
        CFG.events.map((e) => h("span", { class: "chip" }, h("i", { style: `background:${p[e.key]}` }), e.label, h("b", { text: fmt(d.totals[e.key]) }))),
        h("span", { class: "chip" }, "Requested", h("b", { text: money(d.values.submitted) })),
        h("span", { class: "chip" }, "Disbursed", h("b", { text: money(d.values.disbursed) })));
      body.replaceChildren(
        chips,
        h("div", { class: "grid g-2" },
          h("div", null, h("h3", { text: "By product" }), h("div", { class: "chart xshort", id: "ch-drawer-products" })),
          h("div", null, h("h3", { text: "By hour of day" }), h("div", { class: "chart xshort", id: "ch-drawer-hours" }))),
        h("h3", { class: "mt", text: "Requests" }),
        requestList(d.rows, { events: true, detailLabel: "Detail" }));
      draw("ch-drawer-products", {
        tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
        grid: grid({ top: 8 }),
        xAxis: valAxis({ minInterval: 1 }),
        yAxis: catAxis(d.by_product.map((r) => r.product), { inverse: true }),
        series: DAILY_KEYS.map((k) => hbar(DAILY_LABELS[k], d.by_product.map((r) => r[k]), p[k], { stack: "s", itemStyle: { color: p[k], borderColor: p.surface, borderWidth: 1 } })),
      });
      const hours = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, "0")}:00`);
      draw("ch-drawer-hours", {
        tooltip: { trigger: "axis" },
        grid: grid({ top: 8 }),
        xAxis: catAxis(hours, { axisLabel: { color: p.muted, fontSize: 10, interval: 3 } }),
        yAxis: valAxis({ minInterval: 1 }),
        series: [bar("Submitted", d.hourly.submitted, p.submitted), bar("Disbursed", d.hourly.disbursed, p.disbursed)],
      });
    } catch (err) {
      body.replaceChildren(h("div", { class: "error-box", text: err.message }));
    }
  }

  // --- deck building blocks --------------------------------------------------

  function tiles(id, t, product) {
    const p = P();
    const tile = (label, value, colour, foot, onClick, title) => h(onClick ? "button" : "div", {
      class: "tile", type: onClick ? "button" : null, onclick: onClick, title,
      style: colour ? `--tile-colour:${colour}` : null,
    }, h("div", { class: "k", text: label }), h("div", { class: "v", text: value }), foot ? h("div", { class: "f", text: foot }) : null);
    const open = (key, colour) => () => openDrill("group", key, { product, colour });
    $(id).replaceChildren(
      tile("Applications", fmt(t.applications), null, `${money(t.requested_value)} requested`, () => openDrill("all", "1", { product, reasons: false }), "See every request"),
      tile("Disbursed", fmt(t.disbursed), p.outcome.DISBURSED, `${money(t.disbursed_value)} · ${pct(t.completion_rate)}`, open("DISBURSED", p.outcome.DISBURSED), "See these requests"),
      tile("In progress", fmt(t.in_progress), p.outcome.IN_PROGRESS, "Reviewed to approved", open("IN_PROGRESS", p.outcome.IN_PROGRESS), "See where they are waiting"),
      tile("Pending", fmt(t.pending), p.outcome.PENDING, "Not yet reviewed", open("PENDING", p.outcome.PENDING), "See these requests"),
      tile("Rejected", fmt(t.rejected), p.outcome.REJECTED, "Click to see why", open("REJECTED", p.outcome.REJECTED), "See why they were rejected"),
      tile("Days to disburse", days(t.median_days_to_disburse), null, "median"));
  }

  /* A pie labelled on the chart itself: each slice names itself with its
     count (or amount) and share, so nothing depends on a separate legend. */
  function pie(chartId, items, opts) {
    opts = opts || {};
    const p = P();
    const total = items.reduce((s, it) => s + it.value, 0);
    const valueText = (v) => (opts.money ? money(v) : fmt(v));
    draw(chartId, {
      tooltip: { trigger: "item", formatter: (it) => tipRows(it.name, [
        { label: opts.money ? "Amount" : "Requests", value: opts.money ? moneyFull(it.value) : fmt(it.value), color: it.color },
        { label: "Share", value: pct(it.percent) }], opts.onClick ? (opts.clickHint || "Click to see the requests") : null) },
      series: [{
        type: "pie", radius: ["0%", "66%"], center: ["50%", "53%"], startAngle: 90, minShowLabelAngle: 0,
        avoidLabelOverlap: true,
        itemStyle: { borderColor: p.surface, borderWidth: 2 },
        label: {
          show: true, position: "outside", color: p.ink, fontSize: 13, lineHeight: 17,
          formatter: (it) => `{name|${it.name}}\n{val|${valueText(it.value)}}  {pct|${total ? pct((it.value * 100) / total) : "–"}}`,
          rich: {
            name: { color: p.ink2, fontSize: 12.5, fontWeight: 600, lineHeight: 17 },
            val: { color: p.ink, fontSize: 14, fontWeight: 800, lineHeight: 18 },
            pct: { color: p.muted, fontSize: 12.5, fontWeight: 600, lineHeight: 18 },
          },
        },
        labelLine: { show: true, length: 10, length2: 12, lineStyle: { color: p.axis, width: 1.5 } },
        labelLayout: { hideOverlap: false, moveOverlap: "shiftY" },
        emphasis: { scale: true, scaleSize: 5 },
        data: items.map((it) => ({ name: it.label, value: it.value, itemStyle: { color: it.colour } })),
      }],
    }, opts.onClick ? { onClick: (it) => opts.onClick(items[it.dataIndex]) } : null);
  }

  function outcomePie(scope, block) {
    const p = P();
    const items = block.outcome.map((o) => ({ key: o.key, label: o.label, value: o.count, colour: p.outcome[o.key] }));
    const product = scope === "all" ? null : scope;
    pie(`ch-outcome-${scope}`, items, {
      onClick: (it) => openDrill("group", it.key, { product, colour: it.colour }),
    });
    // The "See why…" dropdown: every outcome, and the exact statuses inside them.
    const select = document.querySelector(`select.why[data-scope="${scope}"]`);
    if (select) {
      select.replaceChildren(h("option", { value: "", text: "See why…" }),
        h("optgroup", { label: "Outcome" }, block.outcome.map((o) => h("option", { value: `group:${o.key}`, text: `${o.key === "REJECTED" ? "Rejected — why?" : o.label} (${fmt(o.count)})` }))),
        h("optgroup", { label: "Exact status" }, block.statuses.filter((s) => s.count).map((s) => h("option", { value: `status:${s.key}`, text: `${s.label} (${fmt(s.count)})` }))));
      select.onchange = () => {
        const [kind, key] = select.value.split(":");
        select.value = "";
        if (kind) openDrill(kind, key, { product, colour: p.outcome[key] || (key === "CORRECTION_REQUESTED" ? p.correction : key === "REJECTED" ? p.rejected : p.bar) });
      };
    }
  }

  function monthsChart(id, block, product, opts) {
    opts = opts || {};
    const p = P();
    const rows = block.monthly;
    const colour = opts.value ? p.bar2 : (opts.colour || p.bar);
    const values = rows.map((m) => (opts.value ? m.value : m.count));
    draw(id, {
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, formatter: (its) => {
        const m = rows[its[0].dataIndex];
        return tipRows(monthLong(m.key), [
          { label: "Applications", value: fmt(m.count), color: colour },
          { label: "Requested", value: moneyFull(m.value) },
          { label: "Disbursed", value: fmt(m.disbursed) },
        ], "Click to list them");
      } },
      grid: grid({ top: 22 }),
      xAxis: catAxis(rows.map((m) => monthLabel(m.key))),
      yAxis: valAxis(opts.value ? { axisLabel: { color: p.muted, fontSize: 11, formatter: (v) => money(v) } } : { minInterval: 1 }),
      series: [bar(opts.value ? "Value requested" : "Applications", values, colour, {
        label: { show: rows.length <= 20, position: "top", color: p.ink, fontSize: rows.length > 14 ? 10.5 : 12, fontWeight: 650,
          formatter: (it) => (opts.value ? compact(it.value) : fmt(it.value)) },
      })],
    }, { onClick: (it) => openDrill("month", rows[it.dataIndex].key, { product }) });
  }

  /* Short periods (up to ~3 months) show one bar per day; longer ones one per
     month. A month view of "This month" would be a single bar. */
  const DAILY_MAX_DAYS = 92;
  const DAY_CHART_VISIBLE = 31;

  function periodChart(scope, d, colour) {
    const product = scope === "all" ? null : scope;
    const id = `ch-months-${scope}`;
    const days = dayCount(d.window.start, d.window.end);
    const perDay = days <= DAILY_MAX_DAYS;
    $(`period-title-${scope}`).textContent = perDay ? "Applications per day" : "Applications per month";
    $(`period-hint-${scope}`).textContent = perDay ? "Click a day" : "Click a month";
    if (!perDay) {
      monthsChart(id, d.scopes[scope], product, { colour });
      return;
    }
    const p = P();
    const rows = d.daily[scope].days;
    const zoom = rows.length > DAY_CHART_VISIBLE ? [
      { type: "inside", start: 100 - (DAY_CHART_VISIBLE / rows.length) * 100, end: 100 },
      { type: "slider", start: 100 - (DAY_CHART_VISIBLE / rows.length) * 100, end: 100, height: 12, bottom: 0,
        showDataShadow: false, borderColor: p.axis, fillerColor: "rgba(57,135,229,.18)",
        textStyle: { color: p.muted, fontSize: 10 }, labelFormatter: (i) => (rows[i] ? shortDate(rows[i].date) : "") },
    ] : undefined;
    draw(id, {
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, formatter: (its) => {
        const r = rows[its[0].dataIndex];
        return tipRows(weekdayDate(r.date), [
          { label: "Applications", value: fmt(r.submitted), color: colour || p.bar },
          { label: "Requested", value: moneyFull(r.submitted_value) },
          { label: "Disbursed that day", value: fmt(r.disbursed) },
        ], "Click for the day's details");
      } },
      grid: grid({ top: 22, bottom: zoom ? 22 : 4 }),
      dataZoom: zoom,
      xAxis: catAxis(rows.map((r) => shortDate(r.date)), { axisLabel: { color: p.ink2, fontSize: 11, hideOverlap: true } }),
      yAxis: valAxis({ minInterval: 1 }),
      series: [bar("Applications", rows.map((r) => r.submitted), colour || p.bar, {
        barCategoryGap: "22%",
        label: { show: true, position: "top", color: p.ink, fontSize: 11, fontWeight: 650, formatter: (it) => (it.value ? fmt(it.value) : "") },
      })],
    }, { onDay: (i) => rows[i] && openDay(rows[i].date, product) });
  }

  function rankBars(id, items, colour, opts) {
    opts = opts || {};
    const p = P();
    const valueOf = (r) => (opts.money ? r.value : r.count);
    draw(id, {
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, formatter: (its) => {
        const r = items[its[0].dataIndex];
        return tipRows(r.label, [
          opts.money ? { label: "Disbursed value", value: moneyFull(r.value), color: colour } : { label: "Requests", value: fmt(r.count), color: colour },
          opts.money ? { label: "Loans", value: fmt(r.count) } : null,
        ], opts.kind ? "Click to list them" : null);
      } },
      grid: grid({ top: 6, right: opts.money ? 64 : 40 }),
      xAxis: valAxis(opts.money ? { axisLabel: { color: p.muted, fontSize: 11, formatter: (v) => money(v) } } : { minInterval: 1 }),
      yAxis: catAxis(items.map((r) => r.label), { inverse: true, axisLabel: { color: p.ink2, fontSize: 12, width: opts.labelWidth || 110, overflow: "truncate" } }),
      series: [hbar(opts.money ? "Disbursed value" : "Requests", items.map(valueOf), colour, {
        label: { show: true, position: "right", color: p.ink, fontSize: 12, fontWeight: 650, formatter: (it) => (opts.money ? money(it.value) : fmt(it.value)) } })],
    }, opts.kind ? { onClick: (it) => openDrill(opts.kind, items[it.dataIndex].key, { product: opts.product }) } : null);
  }

  // --- OVERVIEW: all requests ------------------------------------------------

  function renderOverview(d) {
    const p = P();
    const all = d.scopes.all;
    $("overview-sub").textContent = `${fmt(all.tiles.applications)} requests · ${money(all.tiles.requested_value)} requested · ${periodText()}`;
    tiles("tiles-all", all.tiles, null);

    const products = d.products;
    pie("ch-type-pie", products.map((r) => ({ key: r.key, label: r.label, value: r.count, colour: productColour(r.key) })), {
      onClick: (it) => selectTab(`product-${it.key}`), clickHint: "Click to open this product's page",
    });
    outcomePie("all", all);

    // Loan volume by product: hovering ranks each product and marks the
    // highest; the highest bar carries a star too.
    const ranked = products.slice().sort((a, b) => b.value - a.value);
    const rankOf = (key) => ranked.findIndex((r) => r.key === key) + 1;
    const totalValue = products.reduce((s, r) => s + r.value, 0);
    draw("ch-volume-product", {
      legend: { data: ["Requested", "Disbursed"] },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, formatter: (its) => {
        const r = products[its[0].dataIndex];
        const rank = rankOf(r.key);
        return tipRows(`${r.label}${rank === 1 ? "  ★ Highest" : ""}`, [
          { label: "Requested", value: moneyFull(r.value), color: productColour(r.key) },
          { label: "Disbursed", value: moneyFull(r.disbursed_value), color: p.disbursed },
          { label: "Share of all requested", value: pct(totalValue ? (r.value * 100) / totalValue : null) },
          { label: "Rank", value: `${rank} of ${products.length}` },
        ], rank === 1 ? "The largest loan book" : `${money(ranked[0].value - r.value)} behind ${ranked[0].label}`);
      } },
      grid: grid({ top: 30 }),
      xAxis: catAxis(products.map((r) => r.label), { axisLabel: { color: p.ink, fontSize: 13, fontWeight: 600 } }),
      yAxis: valAxis({ axisLabel: { color: p.muted, fontSize: 11, formatter: (v) => money(v) } }),
      series: [
        bar("Requested", products.map((r) => ({ value: r.value, itemStyle: { color: productColour(r.key), borderRadius: [5, 5, 0, 0] } })), p.bar, {
          barGap: "8%",
          label: { show: true, position: "top", color: p.ink, fontSize: 13, fontWeight: 700,
            formatter: (it) => `${money(it.value)}${rankOf(products[it.dataIndex].key) === 1 ? " ★" : ""}` },
        }),
        bar("Disbursed", products.map((r) => r.disbursed_value), p.disbursed, {
          itemStyle: { color: p.disbursed, opacity: 0.85, borderRadius: [5, 5, 0, 0] },
          label: { show: true, position: "top", color: p.ink2, fontSize: 12, formatter: (it) => money(it.value) },
        }),
      ],
    }, { onClick: (it) => selectTab(`product-${products[it.dataIndex].key}`) });

    periodChart("all", d);
  }

  // --- MONTHLY OVERVIEW --------------------------------------------------------

  function renderMonthly(d) {
    const p = P();
    const all = d.scopes.all;
    const months = all.monthly;
    $("monthly-sub").textContent = `${fmt(all.tiles.applications)} applications · ${money(all.tiles.requested_value)} requested · ${periodText()}`;
    const busiest = months.reduce((best, m) => (m.count > (best ? best.count : -1) ? m : best), null);
    const active = months.filter((m) => m.count).length || 1;
    const tile = (label, value, foot) => h("div", { class: "tile" }, h("div", { class: "k", text: label }), h("div", { class: "v", text: value }), foot ? h("div", { class: "f", text: foot }) : null);
    $("tiles-monthly").replaceChildren(
      tile("Months", fmt(months.length), periodText()),
      tile("Average a month", fmt(all.tiles.applications / active), `${money(all.tiles.requested_value / active)} requested`),
      tile("Busiest month", busiest && busiest.count ? monthLong(busiest.key) : "–", busiest ? `${fmt(busiest.count)} applications` : ""),
      tile("Top officer", all.officers[0] ? all.officers[0].label : "–", all.officers[0] ? `${money(all.officers[0].value)} disbursed` : ""));
    drawMonthlyMain();
    rankBars("ch-bands-all", all.bands, p.bar, { kind: "band" });
    rankBars("ch-states-all", all.states, p.bar2, { kind: "state" });
    rankBars("ch-officers-all", all.officers, p.credit_approved, { money: true, kind: "officer", labelWidth: 120 });
  }

  function drawMonthlyMain() {
    const value = state.monthlyMode === "value";
    $("monthly-title").textContent = value ? "Values requested per month (₦)" : "Applications per month";
    document.querySelectorAll("#monthly-mode button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === state.monthlyMode)));
    monthsChart("ch-monthly", state.data.scopes.all, null, { value });
  }

  // --- PRODUCT PAGES -----------------------------------------------------------

  function renderProduct(d, code) {
    const p = P();
    const block = d.scopes[code];
    $(`sub-${code}`).textContent = `${fmt(block.tiles.applications)} applications · ${money(block.tiles.requested_value)} requested · ${periodText()}`;
    tiles(`tiles-${code}`, block.tiles, code);
    outcomePie(code, block);
    periodChart(code, d, productColour(code));

    if (code !== "cash-for-car") {
      rankBars(`ch-bands-${code}`, block.bands, productColour(code), { kind: "band", product: code });
      rankBars(`ch-states-${code}`, block.states, productColour(code), { kind: "state", product: code });
      return;
    }
    // Cash for Car keeps to four charts: loan size and top states share a
    // card (a switch on the card), and the Dash vs Floauto split takes the
    // fourth, with the per-loan "who took the higher share" counts under it.
    drawDist(code);
    const c = d.commission;
    pie("ch-split-pie", [
      { key: "dash", label: "Dash", value: c.dash, colour: p.dash },
      { key: "floauto", label: "Floauto", value: c.floauto, colour: p.floauto },
    ], { money: true });
    const item = (colour, label, n) => h("span", null, h("i", { style: `background:${colour}` }), `${label} `, h("b", { text: fmt(n) }));
    $("split-foot").replaceChildren(
      h("span", { class: "muted", text: `Higher share on ${fmt(c.loans)} disbursed loans:` }),
      item(p.dash, "Dash", c.dash_higher), item(p.floauto, "Floauto", c.floauto_higher), item(p.equal, "Equal", c.equal));
  }

  function drawDist(code) {
    const block = state.data.scopes[code];
    const states = state.distMode === "states";
    $(`dist-title-${code}`).textContent = states ? "Top states" : "Loan size distribution";
    document.querySelectorAll(`.dist-mode[data-product="${code}"] button`).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === state.distMode)));
    rankBars(`ch-dist-${code}`, states ? block.states : block.bands, productColour(code), { kind: states ? "state" : "band", product: code });
  }

  // --- DAILY ACTIVITY ----------------------------------------------------------

  /* Daily charts show DAILY_VISIBLE days at a time (the latest first) with a
     slider to move back, so each day's bars stay wide enough to read. */
  function dayZoom(rows) {
    const p = P();
    if (rows.length <= DAILY_VISIBLE) return undefined;
    const startPct = 100 - (DAILY_VISIBLE / rows.length) * 100;
    return [
      { type: "inside", start: startPct, end: 100 },
      { type: "slider", start: startPct, end: 100, height: 14, bottom: 0, showDataShadow: false, borderColor: p.axis, fillerColor: "rgba(57,135,229,.18)",
        textStyle: { color: p.muted, fontSize: 10 }, labelFormatter: (i) => (rows[i] ? shortDate(rows[i].date) : "") },
    ];
  }

  function drawDaily(id, rows, mode, product) {
    const p = P();
    const zoom = dayZoom(rows);
    draw(id, {
      legend: { data: DAILY_KEYS.map((k) => DAILY_LABELS[k]) },
      tooltip: { trigger: "axis", axisPointer: { type: mode === "line" ? "line" : "shadow" }, formatter: (items) => {
        const r = rows[items[0].dataIndex];
        return tipRows(weekdayDate(r.date), DAILY_KEYS.map((k) => ({ label: DAILY_LABELS[k], value: fmt(r[k]), color: p[k] })), "Click for the day's details");
      } },
      grid: grid({ top: 30, bottom: zoom ? 26 : 4 }),
      dataZoom: zoom,
      xAxis: catAxis(rows.map((r) => `${WEEKDAYS[parseDay(r.date).getDay()]} ${shortDate(r.date)}`), { axisLabel: { color: p.ink2, fontSize: 11.5, hideOverlap: true } }),
      yAxis: valAxis({ minInterval: 1 }),
      series: DAILY_KEYS.map((k) => (mode === "line"
        ? line(DAILY_LABELS[k], rows.map((r) => r[k]), p[k], { showSymbol: true })
        : bar(DAILY_LABELS[k], rows.map((r) => r[k]), p[k], { barCategoryGap: "20%", barGap: "6%", barMaxWidth: 40,
          label: { show: true, position: "top", color: p.ink2, fontSize: 10.5, formatter: (it) => (it.value ? it.value : "") } }))),
    }, { onDay: (i) => rows[i] && openDay(rows[i].date, product) });
  }

  function renderDaily(d) {
    const p = P();
    const product = state.dailyProduct;
    const rows = d.daily[product].days;
    drawDaily("ch-daily", rows, state.dailyMode, product);

    const table = $("daily-table");
    table.replaceChildren(
      h("thead", null, h("tr", null, h("th", { text: "Day" }), DAILY_KEYS.map((k) => h("th", { class: "n" }, h("span", { class: "ev" }, h("i", { style: `background:${p[k]}` }), DAILY_LABELS[k]))))),
      h("tbody", null, rows.slice().reverse().filter((r) => DAILY_KEYS.some((k) => r[k])).map((r) => h("tr", {
        class: "click day-row", tabindex: "0", onclick: () => openDay(r.date, product),
        onkeydown: (e) => { if (e.key === "Enter") openDay(r.date, product); },
      }, h("td", { text: weekdayDate(r.date) }), DAILY_KEYS.map((k) => h("td", { class: `n${r[k] ? "" : " zero"}`, text: fmt(r[k]) }))))));

    const recent = rows.slice(-91);
    const maxSub = Math.max(1, ...recent.map((r) => r.submitted));
    $("calendar-note").textContent = `Submissions, last ${recent.length} days · ${theme() === "dark" ? "brighter" : "darker"} is busier`;
    draw("ch-calendar", {
      tooltip: { formatter: (it) => {
        const r = recent.find((x) => x.date === it.data[0]);
        return r ? tipRows(weekdayDate(r.date), DAILY_KEYS.map((k) => ({ label: DAILY_LABELS[k], value: fmt(r[k]), color: p[k] })), "Click for the day's details") : "";
      } },
      visualMap: { show: false, min: 0, max: maxSub, inRange: { color: [p.calEmpty, ...p.seq] } },
      calendar: {
        range: recent.length ? [recent[0].date, recent[recent.length - 1].date] : CFG.today,
        top: 22, left: 30, right: 8, bottom: 6, cellSize: ["auto", "auto"],
        itemStyle: { color: p.calEmpty, borderColor: p.surface, borderWidth: 3 },
        splitLine: { show: false }, yearLabel: { show: false },
        dayLabel: { firstDay: 1, color: p.muted, fontSize: 10, nameMap: ["S", "M", "T", "W", "T", "F", "S"] },
        monthLabel: { color: p.muted, fontSize: 11 },
      },
      series: [{ type: "heatmap", coordinateSystem: "calendar", data: recent.map((r) => [r.date, r.submitted]) }],
    }, { onClick: (it) => it.data && openDay(it.data[0], product) });
  }

  // --- APPLICATIONS VS DISBURSEMENTS ------------------------------------------

  function renderFlow(d) {
    const p = P();
    const product = state.dailyProduct;
    const f = d.daily[product];
    const rows = f.days;
    const tile = (label, value, colour, foot) => h("div", { class: "tile", style: colour ? `--tile-colour:${colour}` : null },
      h("div", { class: "k", text: label }), h("div", { class: "v", text: value }), foot ? h("div", { class: "f", text: foot }) : null);
    $("flow-tiles").replaceChildren(
      tile("Applications in", fmt(f.total_submitted), p.submitted, money(f.submitted_value)),
      tile("Disbursed", fmt(f.total_disbursed), p.disbursed, money(f.disbursed_value)),
      tile("Rejected", fmt(f.total_rejected), p.rejected),
      tile("Disbursal rate", f.disbursed_per_submitted == null ? "–" : pct(f.disbursed_per_submitted * 100), null, "disbursed per 100 in"),
      tile("Still open", fmt(f.open_at_end), null, "awaiting a decision"));

    $("flow-sub").textContent = `What came in against what was paid out · ${periodText()}`;
    draw("ch-cumulative", {
      legend: { data: ["Submitted (running total)", "Disbursed (running total)"] },
      tooltip: { trigger: "axis" },
      grid: grid({ top: 30 }),
      xAxis: catAxis(rows.map((r) => shortDate(r.date)), { boundaryGap: false }),
      yAxis: valAxis(),
      series: [
        line("Submitted (running total)", rows.map((r) => r.cumulative_submitted), p.submitted, { showSymbol: false, areaStyle: { color: p.submitted, opacity: 0.14 } }),
        line("Disbursed (running total)", rows.map((r) => r.cumulative_disbursed), p.disbursed, { showSymbol: false, areaStyle: { color: p.disbursed, opacity: 0.2 } }),
      ],
    }, { onDay: (i) => rows[i] && openDay(rows[i].date, product) });

    // Values per month once the period is long: daily bars would be slivers.
    let labels, req, disb;
    if (rows.length > 45) {
      const byMonth = new Map();
      for (const r of rows) {
        const key = r.date.slice(0, 7);
        const m = byMonth.get(key) || { req: 0, disb: 0 };
        m.req += r.submitted_value; m.disb += r.disbursed_value;
        byMonth.set(key, m);
      }
      labels = [...byMonth.keys()].map(monthLabel);
      req = [...byMonth.values()].map((m) => m.req);
      disb = [...byMonth.values()].map((m) => m.disb);
    } else {
      labels = rows.map((r) => shortDate(r.date));
      req = rows.map((r) => r.submitted_value);
      disb = rows.map((r) => r.disbursed_value);
    }
    draw("ch-value", {
      legend: { data: ["Requested", "Disbursed"] },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (v) => moneyFull(v) },
      grid: grid({ top: 30 }),
      xAxis: catAxis(labels),
      yAxis: valAxis({ axisLabel: { color: p.muted, fontSize: 11, formatter: (v) => money(v) } }),
      series: [bar("Requested", req, p.submitted), bar("Disbursed", disb, p.disbursed)],
    });
  }

  // --- TOP INSIGHTS ------------------------------------------------------------

  function renderInsights(d) {
    $("insights-sub").textContent = `What stands out, ${periodText()}. Click a card to open the page behind it.`;
    const list = d.insights.slice(0, 9);
    $("insights").replaceChildren(...(list.length ? list.map((i) => h(i.tab ? "button" : "div", {
      type: i.tab ? "button" : null, class: `insight ${i.tone}${i.tab ? "" : " static"}`, onclick: i.tab ? () => selectTab(i.tab) : null,
    }, h("span", { class: "insight-icon", text: i.tone === "bad" ? "!" : i.tone === "good" ? "↑" : "i" }),
      h("div", { class: "insight-title", text: i.title }),
      h("div", { class: "insight-detail", text: i.detail }),
      i.tab ? h("div", { class: "insight-link", text: `Open ${tabName(i.tab)} →` }) : null)) : [h("div", { class: "empty", text: "Nothing notable in this period." })]));
  }
  function tabName(tab) {
    const btn = document.querySelector(`.tab[data-tab="${tab}"]`);
    return btn ? btn.textContent : tab;
  }

  // --- one request's journey, inside the drawer --------------------------------

  let drawerBack = null;   // the list the journey was opened from

  async function openJourney(product, id) {
    const drawer = $("drawer");
    const body = $("drawer-body");
    if (drawer.classList.contains("open") && body.childNodes.length) {
      drawerBack = { title: $("drawer-title").textContent, sub: $("drawer-sub").textContent, nodes: Array.from(body.childNodes), scroll: body.scrollTop };
      $("drawer-back").hidden = false;
    } else {
      openDrawer("", "");
    }
    $("drawer-title").textContent = "Loading…";
    $("drawer-sub").textContent = "";
    body.replaceChildren(h("div", { class: "empty", text: "Loading…" }));
    body.scrollTop = 0;
    try {
      const j = await api(`/api/journey/${encodeURIComponent(product)}/${encodeURIComponent(id)}/`);
      $("drawer-title").textContent = `${j.reference} · ${j.name}`;
      $("drawer-sub").textContent = `${j.product_label} · ${j.status_label}`;
      body.replaceChildren(...renderJourney(j));
    } catch (err) {
      body.replaceChildren(h("div", { class: "error-box", text: err.message }));
    }
  }

  function drawerGoBack() {
    if (!drawerBack) return;
    $("drawer-title").textContent = drawerBack.title;
    $("drawer-sub").textContent = drawerBack.sub;
    $("drawer-body").replaceChildren(...drawerBack.nodes);
    $("drawer-body").scrollTop = drawerBack.scroll;
    drawerBack = null;
    $("drawer-back").hidden = true;
  }

  function renderJourney(j) {
    const icons = { done: "✓", current: "…", blocked: "✕", correction: "!", future: "" };
    const totalText = j.total_days == null ? "" :
      j.status === "DISBURSED" ? `Disbursed in ${days(j.total_days)}` :
      j.status === "REJECTED" ? `Rejected after ${days(j.total_days)}` : `Open for ${days(j.total_days)}`;
    const steps = j.steps.map((s) => h("div", { class: `t-step ${s.state}` },
      h("div", { class: "t-dot", text: icons[s.state] || "" }),
      h("div", { class: "t-label", text: s.label }),
      h("div", { class: "t-when", text: s.at ? dateTime(s.at) :
        s.state === "current" ? `Waiting ${days(s.waiting_days)}` :
        s.state === "blocked" ? "Stopped here" : s.state === "correction" ? "With the applicant" : "Not yet" }),
      s.took_days != null ? h("div", { class: "t-took", text: `took ${days(s.took_days)}` }) : null,
      s.by ? h("div", { class: "t-by", text: s.by }) : null));
    const parts = [
      h("div", { class: "j-facts", style: "margin-top:0" },
        h("span", null, "Amount ", h("b", { text: moneyFull(j.amount) })),
        j.tenor_months ? h("span", null, "Tenor ", h("b", { text: `${j.tenor_months} months` })) : null,
        j.officer ? h("span", null, "Officer ", h("b", { text: j.officer })) : null,
        j.state ? h("span", null, "State ", h("b", { text: j.state })) : null,
        totalText ? h("span", null, h("b", { text: totalText })) : null),
      h("div", { class: "timeline" }, steps),
    ];
    if (j.rejection) {
      parts.push(h("div", { class: "callout bad" }, h("b", { text: `Rejected${j.rejection.gate ? ` at ${j.rejection.gate}` : ""}${j.rejection.at ? ` on ${dateTime(j.rejection.at)}` : ""}${j.rejection.by ? ` by ${j.rejection.by}` : ""}` }),
        j.rejection.reason || "No reason recorded."));
    }
    if (j.correction) {
      parts.push(h("div", { class: "callout warn" }, h("b", { text: `${j.correction.open ? "Waiting on the applicant to correct" : "Was sent back for correction"} · ${j.correction.count} round${j.correction.count === 1 ? "" : "s"}${j.correction.at ? ` · last on ${dateTime(j.correction.at)}` : ""}${j.correction.by ? ` by ${j.correction.by}` : ""}` }),
        j.correction.message || "No message recorded."));
    }
    parts.push(h("h3", { style: "margin-top:16px", text: "Audit trail" }));
    parts.push(j.history.length ? h("ul", { class: "log" }, j.history.map((x) => h("li", null,
      h("span", { class: "when", text: dateTime(x.at) }),
      h("span", { class: "what" }, h("b", { text: x.action.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()) }),
        x.by ? ` · ${x.by}` : "", x.note ? h("div", { class: "note", text: x.note }) : null)))) : h("p", { class: "muted", text: "No audit entries recorded." }));
    if (j.comments.length) {
      parts.push(h("h3", { style: "margin-top:16px", text: "Comments" }));
      parts.push(h("ul", { class: "log" }, j.comments.map((x) => h("li", null,
        h("span", { class: "when", text: dateTime(x.at) }),
        h("span", { class: "what" }, h("b", { text: x.by || "Unknown" }), x.recommendation ? " · recommendation" : "", h("div", { class: "note", text: x.body }))))));
    }
    return parts;
  }

  // --- COMPARE -----------------------------------------------------------------

  function setCompare(mode) {
    if (!$("a-start").value || !$("a-end").value) {
      $("a-end").value = CFG.today;
      $("a-start").value = addDays(CFG.today, -(CFG.defaultDays - 1));
    }
    const aStart = $("a-start").value, aEnd = $("a-end").value;
    const len = dayCount(aStart, aEnd);
    let bStart, bEnd;
    if (mode === "month" || mode === "year") {
      const shift = (iso) => { const d = parseDay(iso); if (mode === "month") d.setMonth(d.getMonth() - 1); else d.setFullYear(d.getFullYear() - 1); return isoDay(d); };
      bStart = shift(aStart); bEnd = shift(aEnd);
    } else {
      bEnd = addDays(aStart, -1); bStart = addDays(bEnd, -(len - 1));
    }
    $("b-start").value = bStart;
    $("b-end").value = bEnd;
    document.querySelectorAll("#compare-presets button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.cmp === mode)));
    loadCompare();
  }

  async function loadCompare() {
    const params = { a_start: $("a-start").value, a_end: $("a-end").value, b_start: $("b-start").value, b_end: $("b-end").value };
    if (Object.values(params).some((v) => !v)) return;
    const seq = (loadCompare.seq = (loadCompare.seq || 0) + 1);
    try {
      const data = await api("/api/compare/", params);
      if (seq !== loadCompare.seq) return;
      state.compare = data;
      renderCompare();
    } catch (err) {
      showError(err.message);
    }
  }

  function renderCompare() {
    const p = P();
    const c = state.compare;
    $("tag-a").style.background = p.a;
    $("tag-b").style.background = p.b;
    const show = (m, v) => (v == null ? "–" : m.kind === "money" ? money(v) : m.kind === "pct" ? pct(v) : m.kind === "days" ? days(v) : fmt(v));
    const keep = new Set(["submitted", "reviewed", "control_approved", "disbursed", "rejected", "submitted_value", "disbursed_value", "completion_rate", "median_days_to_disburse"]);
    $("compare-table").replaceChildren(
      h("thead", null, h("tr", null, h("th", { text: "Metric" }),
        h("th", { class: "n" }, h("span", { class: "ev" }, h("i", { style: `background:${p.a}` }), `${shortDate(c.a.start)} – ${shortDate(c.a.end)}`)),
        h("th", { class: "n" }, h("span", { class: "ev" }, h("i", { style: `background:${p.b}` }), `${shortDate(c.b.start)} – ${shortDate(c.b.end)}`)),
        h("th", { class: "n", text: "Change" }))),
      h("tbody", null, c.metrics.filter((m) => keep.has(m.key)).map((m) => h("tr", null,
        h("td", { text: m.key === "control_approved" ? "Approved" : m.label }), h("td", { class: "n", text: show(m, m.a) }), h("td", { class: "n", text: show(m, m.b) }),
        h("td", { class: "n" }, m.change == null ? "–" : h("span", { class: `delta ${m.direction || "flat"}`,
          text: `${m.change > 0 ? "▲" : m.change < 0 ? "▼" : ""} ${Math.abs(m.change).toFixed(1)}${m.kind === "pct" ? " pts" : "%"}` }))))),
      h("caption", { class: "box-note", style: "caption-side:bottom;text-align:left;padding-top:8px",
        text: "Completion rate follows each period's own requests to today, so the earlier period has had longer to complete." }));

    const metric = state.compareMetric;
    const len = Math.max(c.a.days.length, c.b.days.length);
    const asBars = len <= 31;
    draw("ch-compare-daily", {
      legend: { data: ["Period A", "Period B"] },
      tooltip: { trigger: "axis", axisPointer: { type: asBars ? "shadow" : "line" }, formatter: (items) => {
        const i = items[0].dataIndex; const ra = c.a.days[i], rb = c.b.days[i];
        return tipRows(`Day ${i + 1}`, [
          { label: ra ? weekdayDate(ra.date) : "Period A", value: ra ? fmt(ra[metric]) : "–", color: p.a },
          { label: rb ? weekdayDate(rb.date) : "Period B", value: rb ? fmt(rb[metric]) : "–", color: p.b }]);
      } },
      grid: grid({ top: 30, bottom: len > 45 ? 30 : 4 }),
      dataZoom: len > 45 ? [{ type: "inside" }, { type: "slider", height: 16, bottom: 2, borderColor: p.axis, textStyle: { color: p.muted } }] : undefined,
      xAxis: catAxis(Array.from({ length: len }, (_, i) => `Day ${i + 1}`)), yAxis: valAxis({ minInterval: 1 }),
      series: asBars
        ? [bar("Period A", c.a.days.map((r) => r[metric]), p.a, { barCategoryGap: "20%", barGap: "4%" }), bar("Period B", c.b.days.map((r) => r[metric]), p.b, { barCategoryGap: "20%", barGap: "4%" })]
        : [line("Period A", c.a.days.map((r) => r[metric]), p.a), line("Period B", c.b.days.map((r) => r[metric]), p.b, { lineStyle: { width: 2.5, color: p.b, type: "dashed" } })],
    });
  }

  // --- auto-play ---------------------------------------------------------------

  let playTimer = null;
  function startPlay() {
    state.playing = true;
    $("play").setAttribute("aria-pressed", "true");
    $("play").title = "Stop auto-play";
    clearInterval(playTimer);
    playTimer = setInterval(() => {
      const i = PLAY_TABS.indexOf(state.tab);
      selectTab(PLAY_TABS[(i + 1) % PLAY_TABS.length]);
    }, PLAY_INTERVAL);
    toast(`Auto-play on: next page every ${PLAY_INTERVAL / 1000}s`);
  }
  function pausePlay() {
    if (!state.playing) return;
    state.playing = false;
    clearInterval(playTimer);
    $("play").setAttribute("aria-pressed", "false");
    $("play").title = "Auto-play pages";
    toast("Auto-play paused");
  }

  // --- wiring --------------------------------------------------------------------

  function bind() {
    bindFilters();
    document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => { pausePlay(); selectTab(b.dataset.tab); }));
    $("drawer-close").addEventListener("click", closeDrawer);
    $("scrim").addEventListener("click", closeDrawer);
    $("play").addEventListener("click", () => (state.playing ? pausePlay() : startPlay()));

    document.querySelectorAll("#daily-mode button").forEach((b) => b.addEventListener("click", () => {
      state.dailyMode = b.dataset.mode;
      document.querySelectorAll("#daily-mode button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      if (state.data) drawDaily("ch-daily", state.data.daily[state.dailyProduct].days, state.dailyMode, state.dailyProduct);
    }));
    document.querySelectorAll("#compare-presets button").forEach((b) => b.addEventListener("click", () => setCompare(b.dataset.cmp)));
    for (const id of ["a-start", "a-end", "b-start", "b-end"]) {
      $(id).addEventListener("change", () => {
        document.querySelectorAll("#compare-presets button").forEach((x) => x.setAttribute("aria-pressed", "false"));
        loadCompare();
      });
    }
    document.querySelectorAll("#compare-metric button").forEach((b) => b.addEventListener("click", () => {
      state.compareMetric = b.dataset.metric;
      document.querySelectorAll("#compare-metric button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      if (state.compare) renderCompare();
    }));
    $("drawer-back").addEventListener("click", drawerGoBack);
    document.querySelectorAll("#monthly-mode button").forEach((b) => b.addEventListener("click", () => {
      state.monthlyMode = b.dataset.mode;
      if (state.data) drawMonthlyMain();
    }));
    document.querySelectorAll(".dist-mode button").forEach((b) => b.addEventListener("click", () => {
      state.distMode = b.dataset.mode;
      if (state.data) drawDist(b.closest(".dist-mode").dataset.product);
    }));

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && $("drawer").classList.contains("open")) { if (drawerBack) drawerGoBack(); else closeDrawer(); return; }
      const typing = /^(INPUT|SELECT|TEXTAREA)$/.test((e.target && e.target.tagName) || "");
      if (typing || e.altKey || e.ctrlKey || e.metaKey || $("drawer").classList.contains("open")) return;
      const i = TABS.indexOf(state.tab);
      if (e.key === "ArrowRight") { pausePlay(); selectTab(TABS[(i + 1) % TABS.length]); }
      if (e.key === "ArrowLeft") { pausePlay(); selectTab(TABS[(i - 1 + TABS.length) % TABS.length]); }
    });

    document.addEventListener("themechange", () => {
      disposeAll();
      state.rendered = new Set([...state.rendered].filter((t) => t === "compare"));
      if (state.tab === "compare") { if (state.compare) renderCompare(); }
      else renderTab(state.tab);
    });

    let resizeTimer = null;
    window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(resizeVisible, 120); });
    window.addEventListener("hashchange", () => {
      const before = state.tab;
      readHash();
      if (state.tab !== before) selectTab(state.tab, { quiet: true });
    });
    setInterval(() => { if (!document.hidden) reload(true); }, REFRESH_INTERVAL);
  }

  function start() {
    const warning = $("boot-warning");
    if (warning) warning.remove();
    if (!window.echarts) showError("The chart library (static/vendor/echarts/echarts.min.js) did not load, so charts cannot be drawn. Hard-refresh the page; on a production server run manage.py collectstatic.");
    applyPreset("all");
    readHash();
    bind();
    syncFilterControls();
    selectTab(state.tab, { quiet: true });
    reload();
  }

  if (window.echarts) start();
  else window.addEventListener("load", start);
})();
