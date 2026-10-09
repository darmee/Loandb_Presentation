/* DashMFB Loan Dashboard - the presentation deck.
 *
 * One page, one screen per tab: an overview of all requests, the daily
 * report, a monthly overview, one page per product, applications vs
 * disbursements, a period comparison and, last, the top insights.
 *
 * The charts move. Each one draws itself in - bars rise one after another,
 * lines run left to right, rings sweep round - when its page comes on (sign
 * in, a reload, a change of page) and again whenever fresh figures arrive
 * from the database. See "chart motion" below.
 *
 * The daily report is live: while it is on screen the page asks
 * /api/live/ every few seconds for the steps loans have reached since it
 * last asked, and slips them in at the top of the feed.
 *
 * /api/dashboard/ returns everything the deck pages need in one call; the
 * page draws only the tab on screen (ECharts cannot size a hidden chart).
 * Every clickable chart element opens the drawer with the requests behind
 * it (from /api/drill/ or /api/day/), and every row opens that request's
 * journey. Ctrl+K opens the command palette: find any request by name or
 * reference (/api/search/), or jump to a page, a period or an action.
 *
 * Movement comes from static/js/ui.js (Motion and Lenis): pages rise in
 * and their headline numbers count up, the selected tab and the pressed
 * option of every switch sit on a sliding pill, the drawer springs in, and
 * the drawer and the tables scroll smoothly. All of it is optional - with
 * reduced motion, or if those scripts fail to load, the deck is unchanged.
 *
 * Server data is only ever inserted with textContent, or through esc()
 * inside chart tooltips, never as raw HTML.
 */
(function () {
  "use strict";

  const CFG = JSON.parse(document.getElementById("app-config").textContent);
  const PRODUCT_CODES = CFG.products.map((p) => p.code);
  const PRODUCT_LABELS = Object.fromEntries(CFG.products.map((p) => [p.code, p.label]));
  const TABS = ["overview", "daily", "monthly", ...PRODUCT_CODES.map((c) => `product-${c}`), "flow", "compare", "insights"];
  const PLAY_TABS = ["overview", "daily", "monthly", ...PRODUCT_CODES.map((c) => `product-${c}`), "flow", "insights"];
  const PLAY_INTERVAL = 15000;
  const REFRESH_INTERVAL = 10 * 60 * 1000;

  const state = {
    tab: "overview",
    preset: "all",
    start: null,
    end: null,
    data: null,
    rendered: new Set(),
    dailyProduct: "all",
    compareMetric: "submitted",
    compare: null,
    monthlyMode: "count",
    distMode: "bands",
    playing: false,
  };
  const charts = {};

  // static/js/ui.js. The stand-in keeps the deck working if it did not load.
  const UI = window.UI || {
    motion: false, SPRING: {}, animate: () => null, enter: () => null, stagger: () => 0,
    count: (el, to, format) => { el.textContent = format(to); }, slide: () => {}, smooth: () => null, syncSegs: () => {},
  };

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

  const SVG_NS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs, ...children) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    el.append(...children);
    return el;
  }
  /* A line icon in the style of the ones in the template. */
  const icon = (...paths) => svg("svg", { class: "ico", viewBox: "0 0 24 24", "aria-hidden": "true" }, ...paths.map((d) => svg("path", { d })));
  const ICONS = {
    page: ["M5 12h14M13 6l6 6-6 6"],
    period: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 7v5l3 2"],
    action: ["M13 3L5 13h6l-1 8 8-10h-6z"],
    request: ["M7 3h7l5 5v13H7z", "M14 3v5h5M10 13h6M10 17h4"],
  };

  /* Sum a long series down to at most `n` points, so a two-year daily
     series still draws as a readable trend. */
  function bucket(values, n) {
    if (values.length <= n) return values;
    const out = new Array(n).fill(0);
    values.forEach((v, i) => { out[Math.min(n - 1, Math.floor((i * n) / values.length))] += v; });
    return out;
  }
  /* The trend drawn behind a tile's number. Nothing for fewer than three
     points: two points are a slope, not a trend. */
  function spark(values, colour) {
    if (!values || values.length < 3 || !values.some((v) => v > 0)) return null;
    values = bucket(values, 36);
    const W = 100, H = 36, max = Math.max(...values);
    const pts = values.map((v, i) => [(i / (values.length - 1)) * W, H - 2 - (v / max) * (H - 6)]);
    // A smooth curve through the points (Catmull-Rom as cubic Beziers), with
    // the control points kept inside the box so it never dips below zero.
    const inside = (y) => Math.max(1, Math.min(H - 1, y));
    let d = `M${pts[0][0].toFixed(2)} ${pts[0][1].toFixed(2)}`;
    for (let i = 0; i < pts.length - 1; i += 1) {
      const a = pts[i - 1] || pts[i], b = pts[i], c = pts[i + 1], e = pts[i + 2] || c;
      d += `C${(b[0] + (c[0] - a[0]) / 6).toFixed(2)} ${inside(b[1] + (c[1] - a[1]) / 6).toFixed(2)} ` +
        `${(c[0] - (e[0] - b[0]) / 6).toFixed(2)} ${inside(c[1] - (e[1] - b[1]) / 6).toFixed(2)} ${c[0].toFixed(2)} ${c[1].toFixed(2)}`;
    }
    return svg("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" },
      svg("path", { class: "area", d: `${d}L${W} ${H}L0 ${H}Z`, fill: colour }),
      svg("path", { class: "line", d, stroke: colour }));
  }

  /* replaceChildren that skips what is not there: the DOM's own turns a null
     into the text "null". */
  function fill(el, ...children) {
    el.replaceChildren(...children.flat(Infinity).filter((c) => c != null && c !== false));
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  const nf = new Intl.NumberFormat("en-NG");
  const fmt = (n) => (n == null ? "–" : nf.format(Math.round(n)));
  const pct = (n) => (n == null ? "–" : `${Number(n).toFixed(1).replace(/\.0$/, "")}%`);
  const whole = (n) => (n == null ? "–" : `${Math.round(n)}%`);   // on screen; pct() is for tooltips
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
  function daysLong(n) {
    if (n == null) return "–";
    if (n < 1) { const hrs = Math.max(1, Math.round(n * 24)); return `${hrs} hour${hrs === 1 ? "" : "s"}`; }
    const d = Math.round(n);
    return `${d} day${d === 1 ? "" : "s"}`;
  }
  const moneyFull = (n) => (n == null ? "–" : `₦${nf.format(Math.round(n))}`);

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  function parseDay(iso) { const [y, m, d] = iso.slice(0, 10).split("-").map(Number); return new Date(y, m - 1, d); }
  function isoDay(date) { const p = (n) => String(n).padStart(2, "0"); return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}`; }
  function addDays(iso, n) { const d = parseDay(iso); d.setDate(d.getDate() + n); return isoDay(d); }
  const dayCount = (a, b) => Math.round((parseDay(b) - parseDay(a)) / 86400000) + 1;
  const longDate = (iso) => { const d = parseDay(iso); return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`; };
  const shortDate = (iso) => { const d = parseDay(iso); return `${d.getDate()} ${MONTHS[d.getMonth()]}`; };
  const WEEKDAYS_LONG = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const weekdayLong = (iso) => { const d = parseDay(iso); return `${WEEKDAYS_LONG[d.getDay()]} ${d.getDate()} ${MONTHS_LONG[d.getMonth()]} ${d.getFullYear()}`; };
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
      ink: "#f5f2f8", ink2: "#c6c0cf", muted: "#91899c", grid: "#27212e", axis: "#3b3344",
      surface: "#1b1522", tooltip: "rgba(30,22,38,.94)", track: "rgba(255,255,255,.06)",
    },
    light: {
      submitted: "#2a78d6", reviewed: "#1baf7a", credit_approved: "#4a3aa7", control_approved: "#eda100",
      disbursed: "#008300", rejected: "#e34948", correction: "#eb6834",
      products: ["#2a78d6", "#1baf7a", "#4a3aa7"],
      outcome: { PENDING: "#94A3B8", IN_PROGRESS: "#2a78d6", DISBURSED: "#008300", REJECTED: "#e34948" },
      dash: "#4a3aa7", floauto: "#eb6834", equal: "#94A3B8",
      bar: "#2a78d6", bar2: "#1baf7a", a: "#2a78d6", b: "#eb6834",
      ink: "#1a141d", ink2: "#4e4756", muted: "#786f82", grid: "#ece7ef", axis: "#d2cad8",
      surface: "#ffffff", tooltip: "rgba(255,255,255,.96)", track: "rgba(79,26,96,.07)",
    },
  };
  const theme = () => (document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");
  const P = () => PALETTES[theme()];
  const productColour = (code) => P().products[Math.max(0, PRODUCT_CODES.indexOf(code)) % 3];
  const DISPLAY_FONT = "'Space Grotesk Variable', 'Inter Variable', system-ui, sans-serif";
  const DAILY_KEYS = ["submitted", "reviewed", "control_approved", "disbursed", "rejected"];
  const DAILY_LABELS = { submitted: "Submitted", reviewed: "Reviewed", control_approved: "Approved", disbursed: "Disbursed", rejected: "Rejected" };

  // --- charts ------------------------------------------------------------------

  // --- chart motion ----------------------------------------------------------
  // A chart that is drawn for the first time plays its entrance on its own.
  // One that is already on the page does not: handed new figures, ECharts
  // just nudges the marks to their new sizes, and handed the same figures it
  // does nothing at all. So when a page comes on, or figures arrive from the
  // database, the charts are cleared first and the entrance plays again.
  // `chartMotion.replay` is that switch; `chartMotion.wait` holds every chart back for
  // a moment so it starts once its card has risen into place.

  const REDUCED_MOTION = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const ARRIVE_WAIT = 260;   // ms: a page coming on - let the card arrive first
  const chartMotion = { replay: false, wait: 0 };

  /* Run `render` with every chart it draws playing its entrance from nothing. */
  function withEntrance(wait, render) {
    const before = { replay: chartMotion.replay, wait: chartMotion.wait };
    chartMotion.replay = true;
    chartMotion.wait = wait;
    try { render(); } finally { Object.assign(chartMotion, before); }
  }
  /* One mark after another: the i-th waits `step` ms longer, up to `cap`. */
  function oneByOne(step, cap) {
    const wait = chartMotion.wait;
    return (i) => wait + Math.min(i * step, cap);
  }

  function baseOption(option) {
    const p = P();
    const tooltip = Object.assign({
      backgroundColor: p.tooltip, borderColor: p.axis, borderWidth: 1, padding: [8, 11],
      textStyle: { color: p.ink, fontSize: 13 }, confine: true,
      extraCssText: "border-radius:14px;backdrop-filter:blur(14px);box-shadow:0 18px 40px -12px rgba(0,0,0,.55);",
    }, option.tooltip || {});
    const legend = option.legend ? Object.assign({
      top: 0, left: 0, icon: "roundRect", itemWidth: 11, itemHeight: 11, itemGap: 16,
      textStyle: { color: p.ink2, fontSize: 12.5 }, inactiveColor: p.axis,
    }, option.legend) : undefined;
    return Object.assign({
      backgroundColor: "transparent",
      animation: !REDUCED_MOTION,
      animationDuration: 950,
      animationEasing: "cubicOut",
      animationDelay: chartMotion.wait,
      // Marks that stay on a chart while its figures change move to their new sizes.
      animationDurationUpdate: 550,
      animationEasingUpdate: "cubicInOut",
      textStyle: { fontFamily: "'Inter Variable', system-ui, -apple-system, 'Segoe UI', sans-serif", color: p.ink2 },
    }, option, { tooltip, legend });
  }
  function catAxis(data, extra) {
    const p = P();
    return Object.assign({
      type: "category", data,
      axisLine: { lineStyle: { color: p.grid } }, axisTick: { show: false },
      axisLabel: { color: p.ink2, fontSize: 12, hideOverlap: true },
    }, extra || {});
  }
  function valAxis(extra) {
    const p = P();
    return Object.assign({
      // Three or four gridlines are enough to read a height against.
      type: "value", splitNumber: 3, splitLine: { lineStyle: { color: p.grid, type: [3, 5] } },
      axisLabel: { color: p.muted, fontSize: 11 }, axisLine: { show: false },
    }, extra || {});
  }
  const grid = (extra) => Object.assign({ left: 6, right: 14, top: 26, bottom: 4, containLabel: true }, extra || {});

  // Slim bars with rounded tops: the mark is the data, not the ink around it.
  function bar(name, data, color, extra) {
    return Object.assign({
      name, type: "bar", data, barCategoryGap: "34%", barGap: "12%", barMaxWidth: 30,
      itemStyle: { color, borderRadius: [6, 6, 0, 0] },
      emphasis: { focus: "none" },
      animationDelay: oneByOne(30, 600),   // bars rise one after another, left to right
    }, extra || {});
  }
  function hbar(name, data, color, extra) {
    return bar(name, data, color, Object.assign({ itemStyle: { color, borderRadius: 7 }, barCategoryGap: "40%", barMaxWidth: 14 }, extra || {}));
  }
  function line(name, data, color, extra) {
    return Object.assign({
      name, type: "line", data, smooth: 0.25, symbol: "circle", symbolSize: 7, showSymbol: data.length <= 45,
      lineStyle: { width: 2.5, color }, itemStyle: { color }, emphasis: { focus: "none" },
      // A line is revealed from left to right, unhurried.
      animationDuration: 1300, animationEasing: "cubicInOut", animationDelay: chartMotion.wait,
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
        const ro = new ResizeObserver(() => { if (!chart.isDisposed()) fit(chart); });
        ro.observe(el);
        chart.__ro = ro;
      }
    }
    // Already on the page: wipe it, so what follows is an entrance and not a nudge.
    else if (chartMotion.replay) chart.clear();
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
  /* Fit a chart to its box - but only if the box has changed size. Resizing
     a chart makes ECharts finish whatever animation is in progress at once,
     and a chart is asked to fit every time its page is shown and after every
     load. Fitting one that already fits is what used to make the charts
     appear all at once instead of drawing themselves in. */
  function fit(chart) {
    const el = chart.getDom();
    if (!el || el.offsetParent === null) return;
    if (chart.getWidth() !== el.clientWidth || chart.getHeight() !== el.clientHeight) chart.resize();
  }
  function resizeVisible() {
    for (const chart of Object.values(charts)) fit(chart);
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
  /* `quiet` leaves the loading bar alone: a question asked every few seconds
     should not make the page flicker. */
  async function api(path, params, opts) {
    const quiet = !!(opts && opts.quiet);
    const url = new URL(path, window.location.origin);
    for (const [k, v] of Object.entries(params || {})) if (v != null && v !== "") url.searchParams.set(k, v);
    if (!quiet) {
      pending += 1;
      $("loading").classList.add("on");
    }
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
      if (!quiet) {
        pending -= 1;
        if (!pending) $("loading").classList.remove("on");
      }
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
    fill(box, h("span", { text }), actionLabel ? h("button", { type: "button", class: "btn", text: actionLabel, onclick: action }) : null);
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

  /* The period, as a page's sub-heading. */
  function periodLabel() {
    if (state.preset === "all") return "All time";
    return state.start ? `${longDate(state.start)} – ${longDate(state.end)}` : "";
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
      resetLive(sel.value);
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
    placeTabInk(opts && opts.quiet);
    if (state.playing) runPlayProgress();
    syncLive();
    requestAnimationFrame(() => {
      // Drawn afresh on every arrival, so the charts come in with the page.
      if (tab !== "compare") state.rendered.delete(tab);
      withEntrance(ARRIVE_WAIT, () => renderTab(tab));
      resizeVisible();
      enterPanel(tab);
    });
  }

  /* The pill behind the selected tab slides to it. */
  function placeTabInk(instant) {
    UI.slide($("tab-ink"), document.querySelector('.tab[aria-selected="true"]'), instant);
  }

  /* Bring a page on: its header, tiles and cards rise in one after another
     and the headline numbers count up. Only on arrival - a background
     refresh redraws in place. `numbersOnly` is for data landing on a page
     that is already on screen. */
  function enterPanel(tab, numbersOnly) {
    const panel = $(`panel-${tab}`);
    if (!panel) return;
    UI.syncSegs(panel, true);
    if (!UI.motion) return;
    if (!numbersOnly) {
      UI.enter(panel.querySelectorAll(".deck-title, .head-controls, .compare-controls"), { y: 10, step: 0.03 });
      UI.enter(panel.querySelectorAll(".card"), { y: 26, step: 0.07, delay: 0.1, scale: 0.97, duration: 0.75 });
    }
    UI.enter(panel.querySelectorAll(".tile"), { y: 14, step: 0.04, delay: 0.03, scale: 0.95 });
    UI.enter(panel.querySelectorAll(".insight"), { y: 26, step: 0.055, delay: 0.06, scale: 0.96, duration: 0.7 });
    UI.animate(panel.querySelectorAll(".tile .spark"), { opacity: [0, 1], scaleY: [0.15, 1] },
      { duration: 0.9, delay: UI.stagger(0.04, { startDelay: 0.3 }) });
    UI.animate(panel.querySelectorAll(".tile .meter span"), { scaleX: [0, 1] }, { duration: 0.9, delay: 0.35 });
    // The figures beside a ring arrive as the ring sweeps round.
    UI.enter(panel.querySelectorAll(".legend .row"), { y: 8, step: 0.07, delay: 0.5, duration: 0.5 });
    panel.querySelectorAll(".tile .v").forEach((el) => { if (el.__count) UI.count(el, el.__count.n, el.__count.format); });
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
      // A deck left open overnight: "today" moves on, and so must the
      // period the buttons stand for, or "30 days" quietly ends yesterday.
      if (data.today && data.today !== CFG.today) {
        CFG.today = data.today;
        if (state.preset && state.preset !== "all") { applyPreset(state.preset); return reload(silent); }
      }
      const first = !state.data;
      state.data = data;
      syncLive();
      showError("");
      checkData(data);
      syncFilterControls();
      for (const t of TABS) if (t !== "compare") state.rendered.delete(t);
      const shown = first ? null : tileFigures(state.tab);
      withEntrance(0, () => renderTab(state.tab));
      if (first) enterPanel(state.tab, true);
      else freshFigures(state.tab, shown);
      if (silent && state.compare) loadCompare();
      requestAnimationFrame(resizeVisible);
    } catch (err) {
      if (!silent && seq === reloadSeq) showError(err.message);
    }
  }

  /* What the headline tiles of a page say now, in order. */
  function tileFigures(tab) {
    const panel = $(`panel-${tab}`);
    return panel ? [...panel.querySelectorAll(".tile .v")].map((el) => (el.__count ? el.__count.n : null)) : [];
  }

  /* Fresh figures have been drawn on a page that is on screen: each headline
     number runs on from what it said to what it says now, and the trends and
     the rings' figures come in again with the charts. Nothing is hidden first
     - someone may be reading. */
  function freshFigures(tab, shown) {
    const panel = $(`panel-${tab}`);
    if (!panel || !UI.motion) return;
    panel.querySelectorAll(".tile .v").forEach((el, i) => {
      const was = shown ? shown[i] : null;
      if (el.__count && was != null && was !== el.__count.n) UI.count(el, el.__count.n, el.__count.format, was);
    });
    UI.animate(panel.querySelectorAll(".tile .spark"), { opacity: [0, 1], scaleY: [0.15, 1] }, { duration: 0.9, delay: UI.stagger(0.04) });
    UI.animate(panel.querySelectorAll(".tile .meter span"), { scaleX: [0, 1] }, { duration: 0.9 });
    UI.enter(panel.querySelectorAll(".legend .row"), { y: 6, step: 0.06, delay: 0.25, duration: 0.45 });
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

  let drawerOpen = false;
  let drawerSeq = 0;         // an open during a closing animation cancels its clean-up
  let drawerReturn = null;   // what had focus before the drawer opened

  /* Read or set how far the drawer is scrolled, through Lenis when it runs. */
  function drawerTop(value) {
    const el = $("drawer-scroll");
    if (value == null) return el.scrollTop;
    if (el.__scroll) el.__scroll.to(value); else el.scrollTop = value;
    return value;
  }

  function openDrawer(title, sub) {
    drawerBack = null;
    $("drawer-back").hidden = true;
    $("drawer-title").textContent = title;
    $("drawer-sub").textContent = sub || "";
    const body = $("drawer-body");
    body.replaceChildren(h("div", { class: "empty", text: "Loading…" }));
    drawerTop(0);
    if (!drawerOpen) {
      drawerOpen = true;
      drawerSeq += 1;
      drawerReturn = document.activeElement;
      const drawer = $("drawer");
      drawer.classList.add("open");
      drawer.setAttribute("aria-hidden", "false");
      drawer.inert = false;
      $("scrim").classList.add("open");
      // The page behind is out of reach - keyboard and screen reader alike -
      // until the drawer closes.
      $("app").inert = true;
      UI.animate(drawer, { x: ["100%", "0%"] }, { type: "spring", stiffness: 340, damping: 36 });
      UI.animate($("scrim"), { opacity: [0, 1] }, { duration: 0.25 });
    }
    pausePlay();
    setTimeout(() => $("drawer-close").focus(), 50);
    return body;
  }
  function closeDrawer() {
    if (!drawerOpen) return;
    drawerOpen = false;
    drawerBack = null;
    $("drawer-back").hidden = true;
    const drawer = $("drawer");
    const seq = drawerSeq;
    drawer.setAttribute("aria-hidden", "true");
    drawer.inert = true;
    $("app").inert = false;
    if (drawerReturn && drawerReturn.isConnected && drawerReturn.focus) drawerReturn.focus({ preventScroll: true });
    drawerReturn = null;
    const done = () => {
      if (seq !== drawerSeq) return;
      drawer.classList.remove("open");
      $("scrim").classList.remove("open");
      drawer.style.transform = "";
      $("scrim").style.opacity = "";
      for (const id of ["ch-drawer-products", "ch-drawer-hours"]) {
        if (charts[id]) { if (charts[id].__ro) charts[id].__ro.disconnect(); charts[id].dispose(); delete charts[id]; }
      }
    };
    UI.animate($("scrim"), { opacity: 0 }, { duration: 0.22 });
    const out = UI.animate(drawer, { x: "100%" }, { duration: 0.26, ease: [0.4, 0, 1, 1] });
    if (out) out.then(done); else done();
  }

  /* New drawer content rises in; the reason bars grow to their length. */
  function enterDrawer() {
    const body = $("drawer-body");
    UI.enter(body.children, { y: 10, step: 0.04 });
    UI.animate(body.querySelectorAll(".reasons .fill"), { scaleX: [0, 1] }, { duration: 0.7, delay: UI.stagger(0.035, { startDelay: 0.1 }) });
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
            h("span", { class: "track" }, h("span", { class: "fill", style: `width:${(n * 100) / max}%;background:${opts.colour || P().rejected}` })),
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
        cells.push(h("td", { class: "whitespace-nowrap" }, h("b", { text: r.reference })));
        cells.push(h("td", { text: r.name }));
        cells.push(h("td", { text: r.product_label }));
        cells.push(h("td", { class: "n", text: moneyFull(r.amount) }));
        cells.push(h("td", null, h("span", { class: `pill ${r.status}`, text: r.status_label })));
        if (!opts.events) cells.push(h("td", { class: "num whitespace-nowrap", text: r.submitted ? shortDate(r.submitted) : "–" }));
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
      $("drawer-sub").textContent = `${fmt(d.count)} request${d.count === 1 ? "" : "s"} · ${scope} · ${periodText()}. ` +
        `${d.truncated ? `The newest ${fmt(d.rows.length)} are listed. ` : ""}Click one to see its journey.`;
      const rejected = key === "REJECTED";
      body.replaceChildren(requestList(d.rows, {
        reasons: opts.reasons !== false,
        reasonsTitle: rejected ? "Why they were rejected" : key === "IN_PROGRESS" || key === "PENDING" ? "Where they are waiting" : "Breakdown",
        colour: opts.colour,
        detailLabel: rejected ? "Reason" : "Details",
      }));
      enterDrawer();
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
      enterDrawer();
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
  // The rule on every page: few numbers, each one earning its place. A tile
  // carries one figure; what supports it is drawn rather than written. A
  // chart labels its peak and leaves the rest one hover away.

  /* One headline figure. `value` is text, or counted(n, format) to count up
     to n when the page comes on. `trend` is the line drawn behind the number
     and `meter` (0-100) a bar under it. The series colour is a marker beside
     the label; the number itself stays in the text colour. */
  const counted = (n, format) => ({ n, format });
  function tile(o) {
    const v = h("div", { class: "v" });
    if (o.value && typeof o.value === "object") { v.textContent = o.value.format(o.value.n); v.__count = o.value; }
    else v.textContent = o.value;
    const colour = o.colour || null;
    return h(o.onClick ? "button" : "div", {
      class: "tile", type: o.onClick ? "button" : null, onclick: o.onClick, title: o.title,
      style: colour ? `--tile-colour:${colour}` : null,
    }, h("div", { class: "k" }, colour ? h("i") : null, o.label), v,
      o.meter != null ? h("div", { class: "meter", role: "img", "aria-label": o.meterLabel || whole(o.meter) },
        h("span", { style: `width:${Math.max(0, Math.min(100, o.meter))}%` })) : null,
      o.foot ? h("div", { class: "f", text: o.foot }) : null,
      o.trend ? spark(o.trend, colour || P().bar) : null);
  }

  /* The six headline tiles of a deck page: how many requests, and where they
     stand. Each opens the requests behind it; behind each number runs its
     trend - the requests submitted each month that are in that state today. */
  function tiles(id, block, product) {
    const p = P();
    const t = block.tiles;
    const by = (key) => block.monthly.map((m) => m[key] || 0);
    const open = (key, colour) => () => openDrill("group", key, { product, colour });
    $(id).replaceChildren(
      tile({ label: "Applications", value: counted(t.applications, fmt), foot: `${money(t.requested_value)} requested`, trend: by("count"),
        onClick: () => openDrill("all", "1", { product, reasons: false }), title: "See every request" }),
      tile({ label: "Disbursed", value: counted(t.disbursed, fmt), colour: p.outcome.DISBURSED, foot: `${money(t.disbursed_value)} · ${pct(t.completion_rate)}`,
        trend: by("disbursed"), onClick: open("DISBURSED", p.outcome.DISBURSED), title: "See these requests" }),
      tile({ label: "In progress", value: counted(t.in_progress, fmt), colour: p.outcome.IN_PROGRESS, foot: "Reviewed to approved",
        trend: by("in_progress"), onClick: open("IN_PROGRESS", p.outcome.IN_PROGRESS), title: "See where they are waiting" }),
      tile({ label: "Pending", value: counted(t.pending, fmt), colour: p.outcome.PENDING, foot: "Not yet reviewed",
        trend: by("pending"), onClick: open("PENDING", p.outcome.PENDING), title: "See these requests" }),
      tile({ label: "Rejected", value: counted(t.rejected, fmt), colour: p.outcome.REJECTED, foot: "Click to see why",
        trend: by("rejected"), onClick: open("REJECTED", p.outcome.REJECTED), title: "See why they were rejected" }),
      tile({ label: "Days to disburse", value: days(t.median_days_to_disburse), foot: "median" }));
  }

  /* A ring with its figures beside it, not on it: one row per slice with the
     slice's colour, its name, its number and its share. The ring carries only
     the total, in the middle. A row does what its slice does - and hovering
     one lights the other up. */
  function pie(chartId, items, opts) {
    opts = opts || {};
    const p = P();
    const total = items.reduce((s, it) => s + it.value, 0);
    const valueText = (v) => (opts.money ? money(v) : fmt(v));
    // The rows beside the ring go in before the ring is drawn, so its box is
    // settled by the time it starts to sweep round.
    const legend = $(chartId.replace(/^ch-/, "lg-"));
    const light = (i, on) => {
      const chart = charts[chartId];
      if (chart && !chart.isDisposed()) chart.dispatchAction({ type: on ? "highlight" : "downplay", seriesIndex: 0, dataIndex: i });
    };
    if (legend) {
      fill(legend, items.map((it, i) => h(opts.onClick ? "button" : "div", {
        type: opts.onClick ? "button" : null, class: "row", title: opts.onClick ? (opts.clickHint || "Click to see the requests") : null,
        onclick: opts.onClick ? () => opts.onClick(it) : null,
        onpointerenter: () => light(i, true), onpointerleave: () => light(i, false),
      }, h("i", { class: "sw", style: `background:${it.colour}` }),
        h("span", { class: "name", text: it.label }),
        h("b", { class: "num", text: valueText(it.value) }),
        h("span", { class: "pct", text: total ? pct((it.value * 100) / total) : "–" }))));
    }

    const box = $(chartId);
    const side = box ? Math.min(box.clientWidth, box.clientHeight) : 300;
    draw(chartId, {
      // The whole, in the hole: the slices are its parts.
      title: {
        text: valueText(total), subtext: opts.centre || (opts.money ? "in total" : "requests"), left: "center", top: "middle", itemGap: 1,
        textStyle: { color: p.ink, fontFamily: DISPLAY_FONT, fontWeight: 650, fontSize: Math.round(Math.max(14, Math.min(34, side * 0.1))) },
        subtextStyle: { color: p.muted, fontSize: Math.round(Math.max(9.5, Math.min(13, side * 0.04))) },
      },
      tooltip: { trigger: "item", formatter: (it) => tipRows(it.name, [
        { label: opts.money ? "Amount" : "Requests", value: opts.money ? moneyFull(it.value) : fmt(it.value), color: it.color },
        { label: "Share", value: pct(it.percent) }], opts.onClick ? (opts.clickHint || "Click to see the requests") : null) },
      series: [{
        type: "pie", radius: ["56%", "86%"], center: ["50%", "50%"], startAngle: 90, padAngle: 2.5,
        // The ring sweeps round from the top.
        animationType: "expansion", animationDuration: 1200, animationEasing: "cubicInOut", animationDelay: chartMotion.wait,
        itemStyle: { borderRadius: 7 },
        label: { show: false }, labelLine: { show: false },
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
      grid: grid({ top: 24 }),
      xAxis: catAxis(rows.map((m) => monthLabel(m.key))),
      yAxis: valAxis(opts.value ? { axisLabel: { color: p.muted, fontSize: 11, formatter: (v) => money(v) } } : { minInterval: 1 }),
      series: [bar(opts.value ? "Value requested" : "Applications", values, colour, {
        label: { show: rows.length <= 24, position: "top", distance: 5, color: p.ink, fontSize: rows.length > 14 ? 10.5 : 12, fontWeight: 650,
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
    const values = rows.map((r) => r.submitted);
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
      grid: grid({ top: 24, bottom: zoom ? 22 : 4 }),
      dataZoom: zoom,
      xAxis: catAxis(rows.map((r) => shortDate(r.date)), { axisLabel: { color: p.ink2, fontSize: 11, hideOverlap: true } }),
      yAxis: valAxis({ minInterval: 1 }),
      series: [bar("Applications", values, colour || p.bar, { barCategoryGap: "30%",
        label: { show: true, position: "top", distance: 5, color: p.ink, fontSize: 11, fontWeight: 650, formatter: (it) => (it.value ? fmt(it.value) : "") } })],
    }, { onDay: (i) => rows[i] && openDay(rows[i].date, product) });
  }

  /* A ranking: one thin bar per row on a faint track, its value at the end.
     The value is the only number - there is no axis to read it against. */
  function rankBars(id, items, colour, opts) {
    opts = opts || {};
    const p = P();
    const valueOf = (r) => (opts.money ? r.value : r.count);
    draw(id, {
      tooltip: { trigger: "axis", axisPointer: { type: "none" }, formatter: (its) => {
        const r = items[its[0].dataIndex];
        return tipRows(r.label, [
          opts.money ? { label: "Disbursed value", value: moneyFull(r.value), color: colour } : { label: "Requests", value: fmt(r.count), color: colour },
          opts.money ? { label: "Loans", value: fmt(r.count) } : null,
        ], opts.kind ? "Click to list them" : null);
      } },
      grid: grid({ top: 4, bottom: 2, right: opts.money ? 62 : 38 }),
      xAxis: { type: "value", show: false },
      yAxis: catAxis(items.map((r) => r.label), { inverse: true, axisLine: { show: false },
        axisLabel: { color: p.ink2, fontSize: 12.5, width: opts.labelWidth || 110, overflow: "truncate" } }),
      series: [hbar(opts.money ? "Disbursed value" : "Requests", items.map(valueOf), colour, {
        showBackground: true, backgroundStyle: { color: p.track, borderRadius: 7 },
        label: { show: true, position: "right", distance: 8, color: p.ink, fontSize: 12.5, fontWeight: 650, fontFamily: DISPLAY_FONT,
          formatter: (it) => (opts.money ? money(it.value) : fmt(it.value)) } })],
    }, opts.kind ? { onClick: (it) => openDrill(opts.kind, items[it.dataIndex].key, { product: opts.product }) } : null);
  }

  // --- OVERVIEW: all requests ------------------------------------------------

  function renderOverview(d) {
    const p = P();
    const all = d.scopes.all;
    $("overview-sub").textContent = `${fmt(all.tiles.applications)} requests · ${money(all.tiles.requested_value)} requested · ${periodText()}`;
    tiles("tiles-all", all, null);

    const products = d.products;
    pie("ch-type-pie", products.map((r) => ({ key: r.key, label: r.label, value: r.count, colour: productColour(r.key) })), {
      onClick: (it) => selectTab(`product-${it.key}`), clickHint: "Click to open this product's page",
    });
    outcomePie("all", all);

    // Loan volume by product: every bar carries its figure and the largest
    // book a star; hovering ranks each product.
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
      grid: grid({ top: 34 }),
      xAxis: catAxis(products.map((r) => r.label), { axisLabel: { color: p.ink, fontSize: 13, fontWeight: 600 } }),
      yAxis: valAxis({ axisLabel: { color: p.muted, fontSize: 11, formatter: (v) => money(v) } }),
      series: [
        bar("Requested", products.map((r) => ({ value: r.value, itemStyle: { color: productColour(r.key), borderRadius: [7, 7, 0, 0] } })), p.bar, {
          barGap: "10%", barMaxWidth: 48,
          label: { show: true, position: "top", distance: 6, color: p.ink, fontSize: 13, fontWeight: 650, fontFamily: DISPLAY_FONT,
            formatter: (it) => `${money(it.value)}${totalValue && rankOf(products[it.dataIndex].key) === 1 ? " ★" : ""}` },
        }),
        bar("Disbursed", products.map((r) => r.disbursed_value), p.disbursed, {
          barMaxWidth: 48, itemStyle: { color: p.disbursed, opacity: 0.85, borderRadius: [7, 7, 0, 0] },
          label: { show: true, position: "top", distance: 6, color: p.ink2, fontSize: 12, formatter: (it) => money(it.value) },
        }),
      ],
    }, { onClick: (it) => selectTab(`product-${products[it.dataIndex].key}`) });

    periodChart("all", d);
  }

  // --- DAILY REPORT ------------------------------------------------------------
  // Two things, side by side. On the left, what is happening on each loan as
  // it happens: the page asks the database every few seconds and each new
  // step slides in at the top. On the right, the period day by day.

  const dayTotal = (r) => DAILY_KEYS.reduce((s, k) => s + r[k], 0);
  /* The hours a day was active: its first and last event. */
  const dayHours = (r) => (!r.first_time ? "–" : r.first_time === r.last_time ? r.first_time : `${r.first_time} – ${r.last_time}`);

  function renderDaily(d) {
    drawDayTable(d.daily[state.dailyProduct].days, state.dailyProduct);
    drawLive();
  }

  /* Day by day, newest first: only the days on which something happened.
     Click a day for everything that happened on it. */
  function drawDayTable(rows, product) {
    const p = P();
    const scroller = $("daily-table-scroll");
    const at = scroller.scrollTop;   // a refresh must not throw the reader back to the top
    const active = rows.slice().reverse().filter((r) => dayTotal(r));
    fill($("daily-table"),
      h("thead", null, h("tr", null, h("th", { text: "Day" }), h("th", { text: "Time", title: "From the day's first event to its last" }),
        DAILY_KEYS.map((k) => h("th", { class: "n" }, h("span", { class: "ev" }, h("i", { style: `background:${p[k]}` }), DAILY_LABELS[k]))))),
      h("tbody", null, active.length ? active.map((r) => h("tr", {
        class: "click day-row", tabindex: "0", title: "See everything that happened on this day",
        onclick: () => openDay(r.date, product),
        onkeydown: (e) => { if (e.key === "Enter") openDay(r.date, product); },
      }, h("td", { text: weekdayDate(r.date) }), h("td", { class: "num when", text: dayHours(r) }),
        DAILY_KEYS.map((k) => h("td", { class: `n${r[k] ? "" : " zero"}`, text: fmt(r[k]) }))))
        : h("tr", null, h("td", { class: "empty", colspan: String(DAILY_KEYS.length + 2), text: "Nothing happened in this period." }))));
    if (at) { if (scroller.__scroll) scroller.__scroll.to(at); else scroller.scrollTop = at; }
  }

  // --- the live feed -------------------------------------------------------------
  // /api/live/ answers with the steps reached since the moment it is given.
  // The page keeps the newest LIVE_KEEP, asks again every LIVE_EVERY while the
  // daily report is on screen, and stops asking when it is not.

  const LIVE_EVERY = 5000;       // ms between questions to the database
  const LIVE_KEEP = 60;          // lines kept on the page
  const LIVE_OVERLAP = 30000;    // ms asked for again each time, so a step committed late is not missed
  const LIVE_REFRESH_GAP = 20000;   // ms, at least, between refreshes of the figures new steps set off
  const live = {
    events: [],        // newest first
    seen: new Set(),   // their keys
    latest: null,      // the newest moment the page has, as the server wrote it
    skew: 0,           // server clock minus this computer's, ms
    checked: null,     // when the database last answered (this computer's clock)
    product: "all",
    started: false,    // the first answer has arrived
    failed: false,
    busy: false,
    timer: null,
    ticker: null,
    seq: 0,            // a change of product abandons the answers still on their way
  };
  const liveWanted = () => state.tab === "daily" && !document.hidden && !!state.data;

  /* How long ago, the way a person would say it. Seconds for the first
     minute, minutes for the first hour, then the clock, then the date. */
  function ago(iso) {
    const then = new Date(iso);
    const now = new Date(Date.now() + live.skew);
    const secs = Math.max(0, Math.round((now - then) / 1000));
    if (secs < 5) return "just now";
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
    const p2 = (n) => String(n).padStart(2, "0");
    const clock = `${p2(then.getHours())}:${p2(then.getMinutes())}`;
    const dayOf = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const daysBack = Math.round((dayOf(now) - dayOf(then)) / 86400000);
    if (daysBack === 0) return `Today, ${clock}`;
    if (daysBack === 1) return `Yesterday, ${clock}`;
    return `${then.getDate()} ${MONTHS[then.getMonth()]}${then.getFullYear() === now.getFullYear() ? "" : ` ${then.getFullYear()}`}, ${clock}`;
  }
  const isFresh = (iso) => Date.now() + live.skew - new Date(iso).getTime() < 60000;

  function liveLine(e, fresh) {
    const p = P();
    const meta = [e.reference, e.product_label, moneyFull(e.amount), e.by ? `by ${e.by}` : null, e.detail || null].filter(Boolean).join(" · ");
    return h("li", { "data-key": e.key }, h("button", {
      type: "button", class: `feed-item${fresh ? " is-new" : ""}`, title: "Open this loan's journey", onclick: () => openJourney(e.product, e.id),
    }, h("span", { class: "feed-dot", style: `background:${p[e.event]}` }),
      h("span", { class: "feed-main" },
        h("span", { class: "feed-line" }, h("b", { text: e.label }), ` ${e.name}`),
        h("span", { class: "feed-meta", text: meta })),
      h("time", { datetime: e.at, class: isFresh(e.at) ? "is-fresh" : null, title: dateTime(e.at), text: ago(e.at) })));
  }

  /* Draw the whole feed from what the page holds (first answer, a change of
     product or of theme). New steps after that are slipped in at the top. */
  function drawLive() {
    const feed = $("live-feed");
    if (!live.started) { fill(feed, h("li", { class: "empty", text: "Asking the database…" })); return; }
    if (!live.events.length) { fill(feed, h("li", { class: "empty", text: "Nothing has happened on any loan yet." })); return; }
    fill(feed, live.events.map((e) => liveLine(e, false)));
  }

  function liveStatus() {
    const box = $("live-state");
    box.classList.toggle("is-down", live.failed);
    box.classList.toggle("is-idle", !live.failed && !liveWanted());
    let text;
    if (live.failed) text = "Reconnecting…";
    else if (!live.started) text = "Connecting…";
    else {
      const secs = Math.max(0, Math.round((Date.now() - live.checked) / 1000));
      text = secs < 2 ? "Live · checked just now" : `Live · checked ${secs}s ago`;
    }
    $("live-text").textContent = text;
  }

  /* Once a second: move every "12s ago" on, and the status line with them. */
  function liveTick() {
    document.querySelectorAll("#live-feed time").forEach((t) => {
      t.textContent = ago(t.dateTime);
      t.classList.toggle("is-fresh", isFresh(t.dateTime));
    });
    liveStatus();
  }

  // New steps move the figures too: refresh them, but not more often than
  // every LIVE_REFRESH_GAP however fast the steps arrive.
  let liveRefreshTimer = null;
  let liveRefreshedAt = 0;
  function refreshFiguresSoon() {
    if (liveRefreshTimer) return;
    const wait = Math.max(1500, LIVE_REFRESH_GAP - (Date.now() - liveRefreshedAt));
    liveRefreshTimer = setTimeout(() => { liveRefreshTimer = null; liveRefreshedAt = Date.now(); reload(true); }, wait);
  }

  async function pollLive() {
    if (live.busy || !liveWanted()) return;
    live.busy = true;
    const seq = live.seq;
    const product = live.product !== "all" ? live.product : "";
    // Ask from a little before the newest moment held: a step whose
    // transaction commits late carries an earlier timestamp than ones already
    // seen, and would otherwise never be asked for. Repeats are dropped by key.
    const since = live.latest ? new Date(Date.parse(live.latest) - LIVE_OVERLAP).toISOString() : null;
    try {
      const data = await api("/api/live/", { since, product, limit: LIVE_KEEP }, { quiet: true });
      if (seq !== live.seq) return;
      live.skew = Date.parse(data.now) - Date.now();
      live.checked = Date.now();
      live.failed = false;
      const fresh = data.events.filter((e) => !live.seen.has(e.key));
      fresh.forEach((e) => live.seen.add(e.key));
      if (data.latest && (!live.latest || Date.parse(data.latest) > Date.parse(live.latest))) live.latest = data.latest;
      const first = !live.started;
      live.started = true;
      if (first) {
        live.events = fresh.slice(0, LIVE_KEEP);
        drawLive();
        UI.enter($("live-feed").querySelectorAll("li"), { y: 10, step: 0.02, duration: 0.45 });
      } else if (fresh.length) {
        live.events = [...fresh, ...live.events].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
        live.events.splice(LIVE_KEEP).forEach((e) => live.seen.delete(e.key));
        addLiveLines(fresh);
        refreshFiguresSoon();
      }
    } catch (err) {
      if (seq === live.seq) live.failed = true;
    } finally {
      live.busy = false;
      liveStatus();
    }
  }

  /* Slip the new steps in where they belong (almost always the top), let the
     oldest lines fall off the end, and draw the eye to what arrived. */
  function addLiveLines(fresh) {
    const feed = $("live-feed");
    const placeholder = feed.querySelector("li.empty");
    if (placeholder) placeholder.remove();
    const added = [];
    for (const e of fresh.slice().sort((a, b) => Date.parse(a.at) - Date.parse(b.at))) {
      const index = live.events.indexOf(e);
      if (index < 0) continue;   // older than everything kept
      const line = liveLine(e, true);
      const next = live.events[index + 1];
      const before = next ? feed.querySelector(`li[data-key="${CSS.escape(next.key)}"]`) : null;
      feed.insertBefore(line, before);
      added.push(line);
    }
    const keep = new Set(live.events.map((e) => e.key));
    feed.querySelectorAll("li[data-key]").forEach((li) => { if (!keep.has(li.dataset.key)) li.remove(); });
    UI.animate(added, { opacity: [0, 1], y: [-18, 0], scale: [0.98, 1] }, { type: "spring", stiffness: 380, damping: 30, delay: UI.stagger(0.06) });
  }

  /* Start asking when the daily report comes on screen, stop when it goes. */
  function syncLive() {
    const wanted = liveWanted();
    if (wanted && !live.timer) {
      pollLive();
      live.timer = setInterval(pollLive, LIVE_EVERY);
      live.ticker = setInterval(liveTick, 1000);
    } else if (!wanted && live.timer) {
      clearInterval(live.timer);
      clearInterval(live.ticker);
      live.timer = live.ticker = null;
    }
    liveStatus();
  }

  /* A change of product: forget what is held and start again. */
  function resetLive(product) {
    live.seq += 1;
    live.product = product;
    live.events = [];
    live.seen.clear();
    live.latest = null;
    live.started = false;
    live.failed = false;
    live.busy = false;
    drawLive();
    if (live.timer) pollLive();
    liveStatus();
  }

  // --- MONTHLY OVERVIEW --------------------------------------------------------

  function renderMonthly(d) {
    const p = P();
    const all = d.scopes.all;
    const months = all.monthly;
    $("monthly-sub").textContent = periodLabel();
    const busiest = months.reduce((best, m) => (m.count > (best ? best.count : -1) ? m : best), null);
    const active = months.filter((m) => m.count).length || 1;
    $("tiles-monthly").replaceChildren(
      tile({ label: "A typical month", value: counted(all.tiles.applications / active, fmt), foot: "applications", trend: months.map((m) => m.count) }),
      tile({ label: "Busiest month", value: busiest && busiest.count ? monthLong(busiest.key) : "–", foot: busiest && busiest.count ? `${fmt(busiest.count)} applications` : "" }),
      tile({ label: "Top officer", value: all.officers[0] ? all.officers[0].label : "–", foot: all.officers[0] ? `${money(all.officers[0].value)} paid out` : "" }));
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
    tiles(`tiles-${code}`, block, code);
    outcomePie(code, block);
    periodChart(code, d, productColour(code));

    if (code !== "cash-for-car") {
      rankBars(`ch-bands-${code}`, block.bands, productColour(code), { kind: "band", product: code });
      rankBars(`ch-states-${code}`, block.states, productColour(code), { kind: "state", product: code });
      return;
    }
    // Cash for Car keeps to four charts: loan size and top states share a
    // card (a switch on the card), and the Dash vs Floauto split takes the
    // fourth, with who usually takes the larger share said in a sentence.
    drawDist(code);
    const c = d.commission;
    // The sentence first: it sets the height left for the ring, and a chart
    // whose box changes while it is drawing itself in is cut short.
    const ahead = c.dash_higher === c.floauto_higher ? null : (c.dash_higher > c.floauto_higher ? "dash" : "floauto");
    fill($("split-foot"),
      ahead ? h("i", { style: `background:${p[ahead]}` }) : null,
      !c.loans ? "No disbursed loans in this period."
        : ahead ? `${ahead === "dash" ? "Dash" : "Floauto"} took the larger share on ${fmt(Math.max(c.dash_higher, c.floauto_higher))} of ${fmt(c.loans)} loans.`
          : "Dash and Floauto came out even, loan for loan.");
    pie("ch-split-pie", [
      { key: "dash", label: "Dash", value: c.dash, colour: p.dash },
      { key: "floauto", label: "Floauto", value: c.floauto, colour: p.floauto },
    ], { money: true });
  }

  function drawDist(code) {
    const block = state.data.scopes[code];
    const states = state.distMode === "states";
    $(`dist-title-${code}`).textContent = states ? "Top states" : "Loan size distribution";
    document.querySelectorAll(`.dist-mode[data-product="${code}"] button`).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === state.distMode)));
    rankBars(`ch-dist-${code}`, states ? block.states : block.bands, productColour(code), { kind: states ? "state" : "band", product: code });
  }

  // --- APPLICATIONS VS DISBURSEMENTS ------------------------------------------

  function renderFlow(d) {
    const p = P();
    const product = state.dailyProduct;
    const f = d.daily[product];
    const rows = f.days;
    $("flow-tiles").replaceChildren(
      tile({ label: "Received", value: counted(f.total_submitted, fmt), colour: p.submitted, trend: rows.map((r) => r.submitted) }),
      tile({ label: "Disbursed", value: counted(f.total_disbursed, fmt), colour: p.disbursed, trend: rows.map((r) => r.disbursed) }),
      tile({ label: "Still open", value: counted(f.open_at_end, fmt), foot: "awaiting a decision" }));

    $("flow-sub").textContent = periodLabel();
    draw("ch-cumulative", {
      legend: { data: ["Submitted (running total)", "Disbursed (running total)"] },
      tooltip: { trigger: "axis" },
      grid: grid({ top: 34 }),
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
      grid: grid({ top: 34 }),
      xAxis: catAxis(labels),
      yAxis: valAxis({ axisLabel: { color: p.muted, fontSize: 11, formatter: (v) => money(v) } }),
      series: [bar("Requested", req, p.submitted, { barMaxWidth: 16 }), bar("Disbursed", disb, p.disbursed, { barMaxWidth: 16 })],
    });
  }

  // --- TOP INSIGHTS ------------------------------------------------------------

  const INSIGHT_LIMIT = 6;   // the six that matter most, at a size that reads across a room

  function renderInsights(d) {
    $("insights-sub").textContent = "What stands out. Click a card to open the page behind it.";
    const list = d.insights.slice(0, INSIGHT_LIMIT);
    $("insights").replaceChildren(...(list.length ? list.map((i, n) => h(i.tab ? "button" : "div", {
      type: i.tab ? "button" : null, class: `insight ${i.tone}${i.tab ? "" : " static"}`, onclick: i.tab ? () => selectTab(i.tab) : null,
    }, h("div", { class: "insight-top" },
        h("span", { class: "insight-icon", text: i.tone === "bad" ? "!" : i.tone === "good" ? "↑" : "i" }),
        h("span", { class: "insight-rank", text: String(n + 1).padStart(2, "0") })),
      h("div", { class: "insight-title", text: i.title }),
      h("div", { class: "insight-detail", text: i.detail }),
      i.tab ? h("div", { class: "insight-link", text: `Open ${tabName(i.tab)} →` }) : null)) : [h("div", { class: "empty", text: "Nothing notable in this period." })]));
  }
  function tabName(tab) {
    // The full name; the rail may show a shorter label.
    const btn = document.querySelector(`.tab[data-tab="${tab}"]`);
    return btn ? btn.title : tab;
  }

  // --- one request's journey, inside the drawer --------------------------------

  let drawerBack = null;   // the list the journey was opened from

  async function openJourney(product, id) {
    const body = $("drawer-body");
    if (drawerOpen && body.childNodes.length) {
      drawerBack = { title: $("drawer-title").textContent, sub: $("drawer-sub").textContent, nodes: Array.from(body.childNodes), scroll: drawerTop() };
      $("drawer-back").hidden = false;
    } else {
      openDrawer("", "");
    }
    $("drawer-title").textContent = "Loading…";
    $("drawer-sub").textContent = "";
    body.replaceChildren(h("div", { class: "empty", text: "Loading…" }));
    drawerTop(0);
    try {
      const j = await api(`/api/journey/${encodeURIComponent(product)}/${encodeURIComponent(id)}/`);
      $("drawer-title").textContent = `${j.reference} · ${j.name}`;
      $("drawer-sub").textContent = `${j.product_label} · ${j.status_label}`;
      body.replaceChildren(...renderJourney(j));
      enterDrawer();
      UI.animate(body.querySelectorAll(".t-dot"), { scale: [0.4, 1], opacity: [0, 1] },
        { type: "spring", stiffness: 420, damping: 22, delay: UI.stagger(0.07, { startDelay: 0.12 }) });
    } catch (err) {
      body.replaceChildren(h("div", { class: "error-box", text: err.message }));
    }
  }

  function drawerGoBack() {
    if (!drawerBack) return;
    $("drawer-title").textContent = drawerBack.title;
    $("drawer-sub").textContent = drawerBack.sub;
    $("drawer-body").replaceChildren(...drawerBack.nodes);
    drawerTop(drawerBack.scroll);
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
      h("div", { class: "j-facts" },
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
    parts.push(h("h3", { class: "mt-5 mb-1", text: "Audit trail" }));
    parts.push(j.history.length ? h("ul", { class: "log" }, j.history.map((x) => h("li", null,
      h("span", { class: "when", text: dateTime(x.at) }),
      h("span", { class: "what" }, h("b", { text: x.action.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase()) }),
        x.by ? ` · ${x.by}` : "", x.note ? h("div", { class: "note", text: x.note }) : null)))) : h("p", { class: "muted", text: "No audit entries recorded." }));
    if (j.comments.length) {
      parts.push(h("h3", { class: "mt-5 mb-1", text: "Comments" }));
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
      withEntrance(0, renderCompare);
    } catch (err) {
      showError(err.message);
    }
  }

  /* Six measures, each as a pair of bars (this period over the other), this
     period's figure and the change. The other period's figure is one hover
     away - a table of both would be three numbers a row. */
  const COMPARE_ROWS = [
    ["submitted", "Applications"], ["disbursed", "Disbursed"], ["rejected", "Rejected"],
    ["disbursed_value", "Value paid out"], ["completion_rate", "Completion rate"], ["median_days_to_disburse", "Time to disburse"],
  ];

  function renderCompare() {
    const p = P();
    const c = state.compare;
    $("tag-a").style.background = p.a;
    $("tag-b").style.background = p.b;
    const show = (m, v) => (v == null ? "–" : m.kind === "money" ? money(v) : m.kind === "pct" ? whole(v) : m.kind === "days" ? daysLong(v) : fmt(v));
    const metric = Object.fromEntries(c.metrics.map((m) => [m.key, m]));
    const range = (x) => `${shortDate(x.start)} – ${shortDate(x.end)}`;
    fill($("compare-list"),
      h("div", { class: "cmp-key" },
        h("span", null, h("i", { style: `background:${p.a}` }), `Period A · ${range(c.a)}`),
        h("span", null, h("i", { style: `background:${p.b}` }), `Period B · ${range(c.b)}`)),
      COMPARE_ROWS.map(([key, name]) => {
        const m = metric[key];
        if (!m) return null;
        const top = Math.max(m.a || 0, m.b || 0) || 1;
        return h("div", { class: "cmp-row", title: `Period A: ${show(m, m.a)} · Period B: ${show(m, m.b)}` },
          h("span", { class: "cmp-name", text: name }),
          h("span", { class: "cmp-bars" },
            h("i", { style: `width:${((m.a || 0) * 100) / top}%;background:${p.a}` }),
            h("i", { style: `width:${((m.b || 0) * 100) / top}%;background:${p.b}` })),
          h("b", { class: "cmp-val", text: show(m, m.a) }),
          m.change == null ? h("span", { class: "delta flat", text: "–" }) : h("span", { class: `delta ${m.direction || "flat"}`,
            text: `${m.change > 0 ? "▲" : m.change < 0 ? "▼" : ""} ${Math.abs(m.change).toFixed(0)}${m.kind === "pct" ? " pts" : "%"}` }));
      }),
      h("p", { class: "box-note", text: "The figure is Period A's; hover a row for both. Completion rate follows each period's own requests to today, so the earlier period has had longer to complete." }));
    UI.animate($("compare-list").querySelectorAll(".cmp-bars i"), { scaleX: [0, 1] }, { duration: 0.7, delay: UI.stagger(0.03) });

    const metricKey = state.compareMetric;
    const len = Math.max(c.a.days.length, c.b.days.length);
    const asBars = len <= 31;
    draw("ch-compare-daily", {
      legend: { data: ["Period A", "Period B"] },
      tooltip: { trigger: "axis", axisPointer: { type: asBars ? "shadow" : "line" }, formatter: (items) => {
        const i = items[0].dataIndex; const ra = c.a.days[i], rb = c.b.days[i];
        return tipRows(`Day ${i + 1}`, [
          { label: ra ? weekdayDate(ra.date) : "Period A", value: ra ? fmt(ra[metricKey]) : "–", color: p.a },
          { label: rb ? weekdayDate(rb.date) : "Period B", value: rb ? fmt(rb[metricKey]) : "–", color: p.b }]);
      } },
      grid: grid({ top: 34, bottom: len > 45 ? 30 : 4 }),
      dataZoom: len > 45 ? [{ type: "inside" }, { type: "slider", height: 16, bottom: 2, borderColor: p.axis, textStyle: { color: p.muted } }] : undefined,
      xAxis: catAxis(Array.from({ length: len }, (_, i) => `Day ${i + 1}`)), yAxis: valAxis({ minInterval: 1 }),
      series: asBars
        ? [bar("Period A", c.a.days.map((r) => r[metricKey]), p.a, { barCategoryGap: "30%", barGap: "10%", barMaxWidth: 12 }), bar("Period B", c.b.days.map((r) => r[metricKey]), p.b, { barCategoryGap: "30%", barGap: "10%", barMaxWidth: 12 })]
        : [line("Period A", c.a.days.map((r) => r[metricKey]), p.a), line("Period B", c.b.days.map((r) => r[metricKey]), p.b, { lineStyle: { width: 2.5, color: p.b, type: "dashed" } })],
    });
  }

  // --- auto-play ---------------------------------------------------------------

  let playTimer = null;
  let playProgress = null;
  /* The ring around the play button: how long until the next page. */
  function runPlayProgress() {
    if (playProgress) playProgress.stop();
    playProgress = UI.animate("#play .play-ring rect", { strokeDashoffset: [100, 0] }, { duration: PLAY_INTERVAL / 1000, ease: "linear" });
  }
  function startPlay() {
    state.playing = true;
    $("play").setAttribute("aria-pressed", "true");
    $("play").title = "Stop auto-play";
    runPlayProgress();
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
    if (playProgress) playProgress.stop();
    playProgress = null;
    UI.animate("#play .play-ring rect", { strokeDashoffset: 100 }, { duration: 0.25 });
    $("play").setAttribute("aria-pressed", "false");
    $("play").title = "Auto-play pages";
    toast("Auto-play paused");
  }

  // --- command palette (Ctrl+K) -------------------------------------------------
  // One box for everything: type a name or reference to find a request and
  // open its journey, or pick a page, a period or an action.

  let paletteOpen = false;
  let paletteRows = [];      // what is listed, in order: { group, label, run, ... }
  let paletteIndex = 0;
  let paletteFound = [];     // requests the server found for the text typed
  let paletteSeq = 0;        // only the newest search may fill the list
  let paletteTimer = null;
  let paletteReturn = null;

  function paletteCommands() {
    const rows = TABS.map((t) => ({ group: "Pages", icon: ICONS.page, label: tabName(t), run: () => { pausePlay(); selectTab(t); } }));
    document.querySelectorAll("#presets button").forEach((b) => rows.push({
      group: "Period", icon: ICONS.period, label: b.textContent, run: () => { applyPreset(b.dataset.preset); reload(); },
    }));
    rows.push({ group: "Actions", icon: ICONS.action, label: state.playing ? "Stop auto-play" : "Start auto-play", run: () => (state.playing ? pausePlay() : startPlay()) });
    rows.push({ group: "Actions", icon: ICONS.action, label: "Switch light / dark", run: () => document.querySelector("[data-theme-toggle]").click() });
    if (document.fullscreenEnabled) rows.push({ group: "Actions", icon: ICONS.action, label: document.fullscreenElement ? "Leave full screen" : "Full screen", run: toggleFullscreen });
    return rows;
  }

  function renderPalette() {
    const q = $("palette-q").value.trim().toLowerCase();
    const found = paletteFound.map((r) => ({
      group: "Requests", icon: ICONS.request, label: r.reference, sub: r.name, request: r, run: () => openJourney(r.product, r.id),
    }));
    const commands = paletteCommands().filter((c) => !q || `${c.label} ${c.group}`.toLowerCase().includes(q));
    paletteRows = [...found, ...commands];
    paletteIndex = Math.max(0, Math.min(paletteIndex, paletteRows.length - 1));
    const list = $("palette-list");
    if (!paletteRows.length) {
      list.replaceChildren(h("div", { class: "empty", text: q.length < 2 ? "Keep typing to search requests." : "Nothing matches. Try a reference number or a name." }));
      return;
    }
    const nodes = [];
    let group = null;
    paletteRows.forEach((row, i) => {
      if (row.group !== group) { group = row.group; nodes.push(h("div", { class: "p-group", text: group })); }
      const r = row.request;
      nodes.push(h("button", {
        type: "button", class: "p-item", role: "option", id: `p-item-${i}`, "aria-selected": String(i === paletteIndex),
        onclick: () => runPalette(i), onpointermove: () => setPaletteIndex(i),
      }, h("span", { class: "p-icon" }, icon(...row.icon)),
        h("span", { class: "p-main" }, row.label, row.sub ? h("small", { text: row.sub }) : null),
        r ? h("span", { class: "p-meta" }, `${r.product_label} · ${money(r.amount)}`, h("span", { class: `pill ${r.status}`, text: r.status_label })) : null));
    });
    list.replaceChildren(...nodes);
  }

  function setPaletteIndex(i, reveal) {
    if (!paletteRows.length) return;
    paletteIndex = (i + paletteRows.length) % paletteRows.length;
    $("palette-list").querySelectorAll(".p-item").forEach((el, n) => el.setAttribute("aria-selected", String(n === paletteIndex)));
    $("palette-q").setAttribute("aria-activedescendant", `p-item-${paletteIndex}`);
    if (reveal) { const el = $(`p-item-${paletteIndex}`); if (el) el.scrollIntoView({ block: "nearest" }); }
  }

  function runPalette(i) {
    const row = paletteRows[i];
    if (!row) return;
    closePalette();
    row.run();
  }

  function searchPalette() {
    const q = $("palette-q").value.trim();
    paletteIndex = 0;
    clearTimeout(paletteTimer);
    const seq = ++paletteSeq;
    if (q.length < 2) { paletteFound = []; renderPalette(); return; }
    renderPalette();   // the commands filter at once; requests follow
    paletteTimer = setTimeout(async () => {
      try {
        const data = await api("/api/search/", { q });
        if (seq !== paletteSeq || !paletteOpen) return;
        paletteFound = data.rows.slice(0, 8);
        renderPalette();
      } catch (err) { /* the list simply keeps the commands */ }
    }, 180);
  }

  function openPalette() {
    if (paletteOpen || drawerOpen) return;
    paletteOpen = true;
    paletteReturn = document.activeElement;
    pausePlay();
    const el = $("palette");
    el.classList.add("open");
    el.inert = false;
    $("palette-scrim").classList.add("open");
    $("app").inert = true;
    $("palette-q").value = "";
    paletteFound = [];
    paletteIndex = 0;
    renderPalette();
    UI.animate(el, { opacity: [0, 1], scale: [0.95, 1], y: [-18, 0] }, { type: "spring", stiffness: 460, damping: 32 });
    UI.animate($("palette-scrim"), { opacity: [0, 1] }, { duration: 0.2 });
    UI.enter($("palette-list").children, { y: 6, step: 0.012, duration: 0.3 });
    $("palette-q").focus();
  }
  function closePalette() {
    if (!paletteOpen) return;
    paletteOpen = false;
    paletteSeq += 1;
    clearTimeout(paletteTimer);
    $("palette").classList.remove("open");
    $("palette").inert = true;
    $("palette-scrim").classList.remove("open");
    $("palette-scrim").style.opacity = "";
    $("app").inert = false;
    if (paletteReturn && paletteReturn.isConnected && paletteReturn.focus) paletteReturn.focus({ preventScroll: true });
    paletteReturn = null;
  }

  // --- full screen ---------------------------------------------------------------

  function toggleFullscreen() {
    if (!document.fullscreenEnabled) return;
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(() => toast("The browser did not allow full screen"));
  }

  // --- wiring --------------------------------------------------------------------

  function bind() {
    bindFilters();
    document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => { pausePlay(); selectTab(b.dataset.tab); }));
    $("drawer-close").addEventListener("click", closeDrawer);
    $("scrim").addEventListener("click", closeDrawer);
    $("play").addEventListener("click", () => (state.playing ? pausePlay() : startPlay()));

    // The live feed asks only while the daily report can be seen.
    document.addEventListener("visibilitychange", syncLive);
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

    $("search-open").addEventListener("click", openPalette);
    $("palette-scrim").addEventListener("click", closePalette);
    $("palette-q").addEventListener("input", searchPalette);
    $("palette-q").addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); setPaletteIndex(paletteIndex + 1, true); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setPaletteIndex(paletteIndex - 1, true); }
      else if (e.key === "Enter") { e.preventDefault(); runPalette(paletteIndex); }
    });
    UI.smooth($("palette-scroll"), $("palette-list"));

    if (document.fullscreenEnabled) {
      $("fullscreen").addEventListener("click", toggleFullscreen);
      document.addEventListener("fullscreenchange", () => {
        $("fullscreen").setAttribute("aria-pressed", String(!!document.fullscreenElement));
        setTimeout(() => { resizeVisible(); placeTabInk(true); }, 200);
      });
    } else {
      $("fullscreen").hidden = true;
    }

    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (paletteOpen) closePalette(); else openPalette();
        return;
      }
      if (e.key === "Escape" && paletteOpen) { closePalette(); return; }
      if (e.key === "Escape" && drawerOpen) { if (drawerBack) drawerGoBack(); else closeDrawer(); return; }
      const typing = /^(INPUT|SELECT|TEXTAREA)$/.test((e.target && e.target.tagName) || "");
      if (typing || e.altKey || e.ctrlKey || e.metaKey || drawerOpen || paletteOpen) return;
      if (e.key === "/") { e.preventDefault(); openPalette(); return; }
      if (e.key.toLowerCase() === "f") { toggleFullscreen(); return; }
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
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { resizeVisible(); placeTabInk(true); UI.syncSegs(document, true); }, 120);
    });
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => placeTabInk(true));

    // Smooth scrolling inside the drawer and the scrolling tables.
    UI.smooth($("drawer-scroll"), $("drawer-body"));
    document.querySelectorAll(".table-scroll, .card-scroll").forEach((el) => UI.smooth(el));
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
    if (window.UI) $("tabs").classList.add("has-ink");
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
