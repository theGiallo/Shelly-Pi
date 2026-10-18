"use strict";
(() => {
  const { $, el, api, toast, fmt, store, initChrome, stream } = SM;

  const RANGES = [
    { id: "1h", label: "1 h", seconds: 3600 },
    { id: "6h", label: "6 h", seconds: 6 * 3600 },
    { id: "24h", label: "24 h", seconds: 86400 },
    { id: "7d", label: "7 d", seconds: 7 * 86400 },
    { id: "30d", label: "30 d", seconds: 30 * 86400 },
  ];
  let rangeId = store.get("range", "24h");
  if (!RANGES.some((r) => r.id === rangeId)) rangeId = "24h";

  const MODES = [
    { id: "power", label: "Power" },
    { id: "energy", label: "Energy" },
    { id: "cost", label: "Cost" },
  ];
  let modeId = store.get("mode", "power");
  if (!MODES.some((m) => m.id === modeId)) modeId = "power";
  let cum = store.get("cum", false) === true;   // running total instead of each moment
  let stacked = store.get("stacked", true) !== false; // all-plugs chart: stacked, or each plug from zero
  let tariff = null; // from /api/state; { enabled: false } until the first answer

  // One colour per plug, by its place in the list, so a plug keeps its colour when others come and go.
  const PALETTE = {
    light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
  };
  const darkMode = () => matchMedia("(prefers-color-scheme: dark)").matches;
  const cumulative = () => cum && modeId !== "power"; // power has no running total; energy is that

  const devices = new Map(); // id -> device (with .live)
  const rows = new Map(); // id -> { root, refs, chart, span }
  let editing = null;

  const midnight = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return Math.floor(d / 1000); };
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  // ---- range buttons -----------------------------------------------------------

  // Built once and updated in place, so keyboard focus stays on the button just pressed.
  function renderRanges() {
    const box = $("#ranges");
    box.replaceChildren(...RANGES.map((r) => el("button", {
      type: "button", "data-range": r.id, "aria-pressed": String(r.id === rangeId), text: r.label,
      onclick: () => {
        rangeId = r.id;
        store.set("range", rangeId);
        for (const b of box.children) b.setAttribute("aria-pressed", String(b.dataset.range === rangeId));
        loadCharts();
      },
    })));
  }

  function renderModes() {
    const box = $("#modes");
    if (!box.children.length) {
      box.replaceChildren(...MODES.map((m) => el("button", {
        type: "button", "data-mode": m.id, text: m.label,
        onclick: () => setMode(m.id),
      })), el("a", { class: "setup", href: "/settings#cost", "data-setup": "cost", hidden: true,
        title: "Give energy a price to see what each plug costs", text: "Set up cost" }));
    }
    const hasCost = !!(tariff && tariff.enabled);
    if (tariff && modeId === "cost" && !hasCost) { modeId = "power"; resetCharts(); }
    for (const b of box.children) {
      if (b.dataset.mode) b.setAttribute("aria-pressed", String(b.dataset.mode === modeId));
      if (b.dataset.mode === "cost") b.hidden = !hasCost;
      if (b.dataset.setup) b.hidden = hasCost || !tariff;   // until the first answer, show neither
    }
    renderViews();
    renderStackStyle();
  }

  function renderViews() {
    const box = $("#views");
    if (!box.children.length) {
      box.replaceChildren(
        el("button", { type: "button", "data-view": "instant", text: "Instant",
          title: "How much at each moment: power in watts, or energy and cost for each period",
          onclick: () => setView(false) }),
        el("button", { type: "button", "data-view": "cumulative", text: "Cumulative",
          title: "Running total from the start of the range",
          onclick: () => setView(true) }),
      );
    }
    for (const b of box.children) b.setAttribute("aria-pressed", String((b.dataset.view === "cumulative") === cumulative()));
  }

  function renderStackStyle() {
    const box = $("#stack-style");
    if (!box.children.length) {
      box.replaceChildren(
        el("button", { type: "button", "data-style": "stacked", text: "Stacked",
          title: "Plugs on top of each other; the top edge is the total", onclick: () => setStacked(true) }),
        el("button", { type: "button", "data-style": "overlaid", text: "Overlaid",
          title: "Each plug drawn from zero, so you can compare them", onclick: () => setStacked(false) }),
      );
    }
    for (const b of box.children) b.setAttribute("aria-pressed", String((b.dataset.style === "stacked") === stacked));
  }

  function setStacked(on) {
    if (on === stacked) return;
    stacked = on;
    store.set("stacked", on);
    renderStackStyle();
    drawStack();
  }

  function setView(on) {
    if (on && modeId === "power") { modeId = "energy"; store.set("mode", modeId); } // the running total of power is energy
    if (on === cumulative() && cum === on) return;
    cum = on;
    store.set("cum", on);
    renderModes();
    resetCharts();
    loadCharts();
  }

  function setMode(id) {
    if (id === modeId) return;
    modeId = id;
    if (id === "power") { cum = false; store.set("cum", false); }
    store.set("mode", id);
    renderModes();
    resetCharts();
    loadCharts();
  }

  function resetCharts() {
    for (const r of rows.values()) { r.chart?.destroy(); r.chart = null; r.refs.chart.replaceChildren(); }
    stack.chart?.destroy(); stack.chart = null; stack.sig = "";
  }

  // ---- rows -----------------------------------------------------------------------

  function buildRow(dev) {
    const refs = {
      led: el("span", { class: "led", "aria-hidden": "true" }),
      name: el("h2"),
      meta: el("p", { class: "meta" }),
      w: el("span", { class: "w" }),
      kwh: el("p", { class: "kwh" }),
      chart: el("div", { class: "chart" }),
      sw: el("button", { type: "button", class: "switch", role: "switch", "aria-checked": "false" }),
      edit: el("button", { type: "button", class: "icon-btn", "aria-label": "Edit plug", text: "⋯" }),
    };
    refs.sw.addEventListener("click", () => onSwitch(devices.get(dev.id), refs.sw));
    refs.edit.addEventListener("click", () => openEdit(devices.get(dev.id)));
    const root = el("article", { class: "plug", "data-id": dev.id },
      el("div", { class: "who" }, refs.led, el("div", {}, refs.name, refs.meta)),
      el("div", { class: "now" }, el("div", {}, refs.w, el("span", { class: "w-unit", text: "W" })), refs.kwh),
      refs.chart,
      el("div", { class: "ctl" }, refs.sw, refs.edit),
    );
    const row = { root, refs, chart: null, span: null, tip: null };
    new ResizeObserver(() => {
      if (row.chart) row.chart.setSize({ width: refs.chart.clientWidth, height: refs.chart.clientHeight });
    }).observe(refs.chart);
    return row;
  }

  function span(...bits) {
    return bits.filter(Boolean).map((t) => el("span", { text: t }));
  }

  function updateRow(dev) {
    const row = rows.get(dev.id);
    if (!row) return;
    const live = dev.live || {};
    const online = !!live.online;
    const on = online && live.output === true;
    const { refs, root } = row;

    root.classList.toggle("is-on", on);
    root.classList.toggle("is-offline", !online && dev.enabled);
    refs.name.textContent = dev.name;
    refs.sw.setAttribute("aria-label", `${dev.name}: power`);
    refs.sw.setAttribute("aria-checked", String(on));
    refs.sw.disabled = !online;
    refs.sw.title = online ? "" : "This plug isn't responding";

    if (!dev.enabled) {
      refs.meta.replaceChildren(...span(dev.room, "Not being monitored"));
    } else if (!online) {
      const text = live.age == null ? "Waiting for the first reading" : `No answer for ${fmt.age(live.age)}`;
      const m = el("span", { text });
      if (live.error) m.title = live.error;
      refs.meta.replaceChildren(...span(dev.room), m);
    } else {
      refs.meta.replaceChildren(...span(
        dev.room,
        live.voltage != null && `${Math.round(live.voltage)} V`,
        live.current != null && `${live.current.toFixed(2)} A`,
        live.temp_c != null && `${Math.round(live.temp_c)} °C`,
        live.rssi != null && `Wi-Fi ${live.rssi} dBm`,
      ));
    }
    refs.w.textContent = online ? fmt.watts(live.power_w) : "–";
    const bits = [];
    if (dev.energy_since_wh != null) bits.push(fmt.energy(dev.energy_since_wh));
    if (tariff && tariff.enabled && dev.cost_since != null) bits.push(fmt.money(dev.cost_since, tariff));
    refs.kwh.textContent = bits.length ? `${bits.join(" · ")} today` : "";
  }

  function reconcile(list) {
    const ids = new Set(list.map((d) => d.id));
    for (const [id, row] of rows) {
      if (!ids.has(id)) { row.chart?.destroy(); row.root.remove(); rows.delete(id); devices.delete(id); lastHistory.delete(id); }
    }
    if (!list.length) $("#stack").hidden = true;
    const host = $("#plugs");
    for (const dev of list) {
      const prev = devices.get(dev.id);
      // keep fresher live data that arrived over the stream while this request was in flight
      devices.set(dev.id, { ...dev, live: dev.live || (prev && prev.live) || null });
      if (!rows.has(dev.id)) rows.set(dev.id, buildRow(dev));
      host.append(rows.get(dev.id).root); // append also reorders
      updateRow(devices.get(dev.id));
    }
    $("#empty").hidden = list.length > 0;
  }

  // ---- totals -----------------------------------------------------------------------

  function renderTotals() {
    const list = [...devices.values()].filter((d) => d.enabled);
    const online = list.filter((d) => d.live && d.live.online);
    const total = online.reduce((s, d) => s + (d.live.power_w || 0), 0);
    $("#total-w").textContent = total === 0 ? "0" : fmt.watts(total);

    const on = online.filter((d) => d.live.output).length;
    const away = list.length - online.length;
    let sub = "No plugs added yet";
    if (list.length) {
      sub = `${on} of ${list.length} ${list.length === 1 ? "plug is" : "plugs are"} on`;
      if (away) sub += `, ${away} not responding`;
    }
    $("#total-sub").textContent = sub;

    const share = $("#share");
    const parts = online.filter((d) => (d.live.power_w || 0) > 0.5);
    share.replaceChildren(...parts.map((d, i) => {
      const s = el("span", { title: `${d.name}: ${fmt.watts(d.live.power_w)} W` });
      s.style.flexGrow = String(d.live.power_w);
      s.style.background = `color-mix(in srgb, var(--accent) ${Math.max(35, 100 - i * 16)}%, var(--panel))`;
      return s;
    }));
    share.hidden = parts.length === 0;
    renderSums();
  }

  function sumItem(label, value, sub) {
    return el("div", { class: "sum" }, el("span", { class: "sum-label", text: label }),
      el("span", { class: "sum-value", text: value }), sub ? el("span", { class: "sum-sub", text: sub }) : null);
  }

  /** Today and the chart's range as energy and (when a price is set) cost, plus the price right now. */
  function renderSums() {
    const box = $("#sums");
    const list = [...devices.values()];
    if (!list.length) { box.hidden = true; return; }
    const hasCost = !!(tariff && tariff.enabled);
    const sum = (f) => list.reduce((a, d) => a + (f(d) || 0), 0);
    const items = [];
    const today = sum((d) => d.energy_since_wh);
    items.push(sumItem("Today", fmt.energy(today), hasCost ? fmt.money(sum((d) => d.cost_since), tariff) : null));
    const rowsLoaded = [...rows.values()].filter((r) => r.sumWh != null);
    if (rowsLoaded.length) {
      const r = RANGES.find((x) => x.id === rangeId);
      const wh = rowsLoaded.reduce((a, r2) => a + r2.sumWh, 0);
      const cost = rowsLoaded.reduce((a, r2) => a + (r2.sumCost || 0), 0);
      items.push(sumItem(`Last ${r.label}`, fmt.energy(wh), hasCost ? fmt.money(cost, tariff) : null));
    }
    if (hasCost && tariff.now) {
      items.push(sumItem("Price now", fmt.price(tariff.now.price, tariff), tariff.now.slot || "Base price"));
    }
    box.replaceChildren(...items);
    box.hidden = false;
  }

  // ---- switching -----------------------------------------------------------------------

  async function onSwitch(dev, btn) {
    const target = btn.getAttribute("aria-checked") !== "true";
    btn.disabled = true;
    btn.setAttribute("aria-checked", String(target));
    try {
      dev.live = await api(`/api/devices/${dev.id}/switch`, { method: "POST", body: { on: target } });
    } catch (e) {
      toast(`Couldn't switch ${dev.name}. ${e.message}`);
    } finally {
      updateRow(dev);
      renderTotals();
    }
  }

  // ---- charts ------------------------------------------------------------------------------

  function placeholder(row, text) {
    row.chart?.destroy();
    row.chart = null;
    row.refs.chart.replaceChildren(el("div", { class: "chart-empty", text }));
  }

  const stepName = (step) => {
    if (step < 3600) return `${Math.round(step / 60)} min`;
    if (step === 3600) return "hour";
    if (step < 86400) return `${Math.round(step / 3600)} h`;
    return step === 86400 ? "day" : `${Math.round(step / 86400)} days`;
  };

  /** The numbers behind a chart, in base units (W, Wh or currency), for the mode and style chosen. */
  function quantity(h) {
    const raw = modeId === "cost" ? h.cost : modeId === "energy" ? h.e_wh : h.p_avg;
    if (!raw) return null;
    if (!cumulative()) return raw;
    let run = 0;
    return raw.map((v) => (run += v || 0));
  }

  /** How to print those numbers: unit label, scale, axis and tooltip text. `max` is the biggest value drawn. */
  function describe(max, step) {
    const cumul = cumulative();
    const per = cumul ? "running total" : `per ${stepName(step)}`;
    if (modeId === "cost") {
      return { scale: 1, bars: !cumul, axisSize: 56, label: `${tariff.currency} ${per}`,
        axis: (v) => v.toLocaleString(undefined, { maximumFractionDigits: 4 }), tip: (v) => fmt.money(v, tariff) };
    }
    if (modeId === "energy") {
      const unit = max >= 1000 ? "kWh" : "Wh";
      const f = (v) => v.toLocaleString(undefined, { maximumFractionDigits: unit === "kWh" ? 2 : 1 });
      return { scale: unit === "kWh" ? 0.001 : 1, bars: !cumul, axisSize: 48, label: `${unit} ${per}`,
        axis: f, tip: (v) => `${f(v)} ${unit}` };
    }
    return { scale: 1, bars: false, axisSize: 40, label: "W",
      axis: (v) => (v >= 1000 ? `${v / 1000}k` : String(v)), tip: (v) => `${fmt.watts(v)} W` };
  }

  const maxOf = (arr) => Math.max(0, ...arr.filter((v) => v != null));
  const yRange = (max) => [0, modeId === "power" ? Math.max(5, max * 1.2) : (max > 0 ? max * 1.15 : 1)];
  const whenText = (t) => new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

  function drawChart(row, h, devId) {
    const { chart: host } = row.refs;
    const base = quantity(h);
    if (!base || !h.p_avg.some((v) => v != null)) { placeholder(row, "No readings in this range yet"); return; }
    const sr = describe(maxOf(base), h.step);
    const vals = base.map((v) => (v == null ? null : v * sr.scale));
    // bars sit in the middle of the period they stand for
    const xs = sr.bars ? h.t.map((t) => t + h.step / 2) : h.t;
    const data = [xs, vals];
    row.times = h.t; row.step = h.step; row.fmt = sr; // read by the axis and tooltip callbacks
    if (row.chart) { row.chart.setData(data, true); row.unitEl.textContent = sr.label; return; }

    const color = colorOf(devId), muted = cssVar("--muted"), line = cssVar("--line");
    host.replaceChildren();
    const tip = el("div", { class: "chart-tip" });
    row.unitEl = el("span", { class: "chart-unit", text: sr.label });
    const font = "12px system-ui, sans-serif";
    const opts = {
      width: host.clientWidth || 300, height: host.clientHeight || 86,
      padding: [8, 4, 0, 0], legend: { show: false },
      cursor: { y: false, drag: { x: false, y: false }, points: { size: 7, show: !sr.bars } },
      scales: {
        x: { time: true, range: () => [row.span.from, row.span.to] },
        y: { range: (u, min, max) => yRange(max || 0) },
      },
      axes: [
        { stroke: muted, font, size: 24, ticks: { show: false }, grid: { show: false } },
        {
          stroke: muted, font, size: sr.axisSize, ticks: { show: false }, grid: { stroke: line, width: 1 },
          values: (u, v) => v.map((x) => row.fmt.axis(x)),
        },
      ],
      series: [{}, sr.bars
        ? { stroke: color, fill: color + "b8", width: 0, points: { show: false },
            paths: uPlot.paths.bars({ size: [0.82, 60], gap: 1 }) }
        : { stroke: color, width: 1.75, fill: color + "24", spanGaps: false, points: { show: false } }],
      hooks: {
        setCursor: [(u) => {
          const i = u.cursor.idx, v = i == null ? null : u.data[1][i];
          if (v == null) { tip.style.display = "none"; return; }
          tip.textContent = `${row.fmt.tip(v)}, ${whenText(row.times[i])}`;
          tip.style.display = "block";
          const x = u.over.offsetLeft + u.cursor.left + 10;
          tip.style.left = `${Math.max(0, Math.min(x, host.clientWidth - tip.offsetWidth))}px`;
        }],
      },
    };
    row.chart = new uPlot(opts, data, host);
    host.append(tip, row.unitEl);
  }

  // ---- all plugs, stacked ------------------------------------------------------------------------

  const stack = { host: $("#stack-chart"), chart: null, sig: "", tip: null, unitEl: null, fmt: null, parts: [], times: [], step: 0, span: null };
  new ResizeObserver(() => {
    if (stack.chart) stack.chart.setSize({ width: stack.host.clientWidth, height: stack.host.clientHeight });
  }).observe(stack.host);

  function colorOf(id) {
    const ids = [...devices.keys()].sort((a, b) => a - b);
    const i = ids.indexOf(id);
    const pal = darkMode() ? PALETTE.dark : PALETTE.light;
    return i >= 0 && i < pal.length ? pal[i] : cssVar("--muted");
  }

  function drawStack() {
    const box = $("#stack");
    const list = [...devices.values()].sort((a, b) => a.id - b.id).filter((d) => lastHistory.has(d.id));
    const first = list.length ? lastHistory.get(list[0].id) : null;
    const parts = [];
    for (const d of list) {
      const h = lastHistory.get(d.id), q = quantity(h);
      if (q && h.t.length === first.t.length && h.step === first.step) parts.push({ id: d.id, name: d.name, q });
    }
    if (parts.length < 2 || [...rows.values()].every((r) => !r.span)) { box.hidden = true; return; }
    // more plugs than colours: the rest are added together as "Other"
    if (parts.length > PALETTE.light.length) {
      const keep = parts.slice(0, PALETTE.light.length - 1), rest = parts.slice(PALETTE.light.length - 1);
      const sum = first.t.map((_, i) => (rest.every((p) => p.q[i] == null) ? null : rest.reduce((a, p) => a + (p.q[i] || 0), 0)));
      parts.splice(0, parts.length, ...keep, { id: -1, name: "Other", q: sum });
    }
    box.hidden = false;
    const n = first.t.length;
    const tops = [];                       // stacked: running sum up to each plug; overlaid: the plug's own values
    let run = new Array(n).fill(0);
    for (const p of parts) {
      run = run.map((v, i) => v + (p.q[i] || 0));
      tops.push(stacked ? run.map((v, i) => (parts.every((x) => x.q[i] == null) ? null : v)) : p.q);
    }
    const total = run.map((v, i) => (parts.every((x) => x.q[i] == null) ? null : v));
    const sr = describe(stacked ? maxOf(total) : Math.max(...tops.map(maxOf)), first.step);
    const bars = sr.bars && stacked;      // overlapping bars can't be told apart, so overlaid is always lines
    const colors = parts.map((p) => (p.id < 0 ? cssVar("--muted") : colorOf(p.id)));
    stack.parts = parts.map((p, k) => ({ ...p, color: colors[k] }));
    stack.total = total; stack.times = first.t; stack.fmt = sr;
    stack.span = [...rows.values()].find((r) => r.span).span;
    box.querySelector("h2").textContent = (cumulative() ? "All plugs together, running total" : "All plugs together");
    $("#stack-legend").replaceChildren(...stack.parts.map((p) => {
      const sw = el("span", { class: "sw", "aria-hidden": "true" });
      sw.style.background = p.color;
      return el("li", {}, sw, p.name);
    }));
    stack.host.setAttribute("aria-label", `Stacked chart of ${stack.parts.map((p) => p.name).join(", ")}: ${sr.label}`);

    // largest first, so each plug's block is drawn over the ones above it
    const xs = bars ? first.t.map((t) => t + first.step / 2) : first.t;
    const scaled = tops.map((t) => t.map((v) => (v == null ? null : v * sr.scale)));
    const data = [xs, ...(stacked ? scaled.reverse() : scaled)];
    const sig = [modeId, cumulative(), darkMode(), stacked, bars, stack.parts.map((p) => p.id + p.color).join()].join("|");
    if (stack.chart && stack.sig === sig) { stack.chart.setData(data, true); stack.unitEl.textContent = sr.label; return; }
    stack.chart?.destroy();
    stack.sig = sig;
    const surface = cssVar("--surface"), muted = cssVar("--muted"), line = cssVar("--line");
    const font = "12px system-ui, sans-serif";
    stack.host.replaceChildren();
    stack.tip = el("div", { class: "chart-tip stack-tip" });
    stack.unitEl = el("span", { class: "chart-unit", text: sr.label });
    const seriesFor = (k) => {
      if (!stacked) return { stroke: colors[k], width: 1.75, fill: colors[k] + "22", spanGaps: false, points: { show: false } };
      return bars
        ? { stroke: colors[k], width: 0, fill: colors[k], points: { show: false }, paths: uPlot.paths.bars({ size: [0.9, 60], gap: 0 }) }
        : { stroke: surface, width: 1.5, fill: colors[k], spanGaps: false, points: { show: false } };
    };
    stack.chart = new uPlot({
      width: stack.host.clientWidth || 300, height: stack.host.clientHeight || 170,
      padding: [8, 4, 0, 0], legend: { show: false },
      cursor: { y: false, drag: { x: false, y: false }, points: { show: false } },
      scales: {
        x: { time: true, range: () => [stack.span.from, stack.span.to] },
        y: { range: (u, min, max) => yRange(max || 0) },
      },
      axes: [
        { stroke: muted, font, size: 24, ticks: { show: false }, grid: { show: false } },
        { stroke: muted, font, size: sr.axisSize, ticks: { show: false }, grid: { stroke: line, width: 1 },
          values: (u, v) => v.map((x) => stack.fmt.axis(x)) },
      ],
      series: [{}, ...(stacked ? parts.map((_, k) => seriesFor(k)).reverse() : parts.map((_, k) => seriesFor(k)))],
      hooks: {
        setCursor: [(u) => {
          const i = u.cursor.idx;
          if (i == null || stack.total[i] == null) { stack.tip.style.display = "none"; return; }
          const f = stack.fmt, sc = f.scale;
          const rowsEl = stack.parts.map((p) => {
            const sw = el("span", { class: "sw" });
            sw.style.background = p.color;
            return el("div", { class: "row" }, sw, el("span", { text: `${p.name}: ${p.q[i] == null ? "–" : f.tip(p.q[i] * sc)}` }));
          });
          stack.tip.replaceChildren(el("div", { text: whenText(stack.times[i]) }), ...(stacked ? rowsEl.reverse() : rowsEl),
            el("div", { class: "total", text: `Total: ${f.tip(stack.total[i] * sc)}` }));
          stack.tip.style.display = "block";
          const x = u.over.offsetLeft + u.cursor.left + 12;
          stack.tip.style.left = `${Math.max(0, Math.min(x, stack.host.clientWidth - stack.tip.offsetWidth))}px`;
        }],
      },
    }, data, stack.host);
    stack.host.append(stack.tip, stack.unitEl);
  }

  const lastHistory = new Map(); // device id -> last answer, to redraw when only the mode changes

  function redrawFromCache() {
    for (const [id, row] of rows) {
      const h = lastHistory.get(id);
      if (h) drawChart(row, h, id);
    }
    drawStack();
  }

  // Energy and cost are drawn as bars. Round periods (30 min, 1 h, 6 h, a day) and no more bars than
  // fit comfortably keep them readable: slicing a day into odd 4-minute pieces makes every on/off cycle a spike.
  const NICE_STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800];
  const barsMode = () => modeId !== "power" && !cumulative();
  function barStep(range, width) {
    const want = range / Math.max(12, Math.floor(width / 9));
    return NICE_STEPS.find((x) => x >= want) || NICE_STEPS[NICE_STEPS.length - 1];
  }

  async function loadChart(id) {
    const row = rows.get(id);
    if (!row) return;
    const to = Math.floor(Date.now() / 1000);
    const from = to - RANGES.find((r) => r.id === rangeId).seconds;
    row.span = { from, to };
    try {
      const width = row.refs.chart.clientWidth || 300;
      const points = barsMode() ? Math.ceil((to - from) / barStep(to - from, width)) : Math.max(60, Math.min(400, width));
      const h = await api(`/api/history?device=${id}&from=${from}&to=${to}&points=${points}`);
      lastHistory.set(id, h);
      row.sumWh = h.e_wh.reduce((a, v) => a + (v || 0), 0);
      row.sumCost = h.cost ? h.cost.reduce((a, v) => a + (v || 0), 0) : null;
      drawChart(row, h, id);
    } catch (e) {
      row.sumWh = null;
      placeholder(row, "Couldn't load history");
    }
  }

  const loadCharts = async () => { await Promise.all([...rows.keys()].map(loadChart)); drawStack(); renderSums(); };

  // ---- editing --------------------------------------------------------------------------------

  const dlg = $("#edit-dialog");

  function openEdit(dev) {
    editing = dev;
    $("#edit-name").value = dev.name;
    $("#edit-room").value = dev.room || "";
    $("#edit-enabled").checked = dev.enabled;
    $("#remove-box").hidden = true;
    $("#remove-purge").checked = false;
    $("#remove-name").textContent = dev.name;
    dlg.showModal();
    $("#edit-name").focus();
  }

  $("#edit-cancel").addEventListener("click", () => dlg.close());
  $("#edit-remove-open").addEventListener("click", () => { $("#remove-box").hidden = false; $("#remove-confirm").focus(); });

  $("#edit-save").addEventListener("click", async () => {
    const name = $("#edit-name").value.trim();
    if (!name) { toast("Give the plug a name."); $("#edit-name").focus(); return; }
    try {
      await api(`/api/devices/${editing.id}`, {
        method: "PATCH", body: { name, room: $("#edit-room").value.trim(), enabled: $("#edit-enabled").checked },
      });
      dlg.close();
      await refresh();
    } catch (e) { toast(e.message); }
  });

  $("#remove-confirm").addEventListener("click", async () => {
    const purge = $("#remove-purge").checked;
    try {
      await api(`/api/devices/${editing.id}?purge=${purge}`, { method: "DELETE" });
      dlg.close();
      toast(`Removed ${editing.name}.`);
      await refresh();
    } catch (e) { toast(e.message); }
  });

  // ---- data flow ---------------------------------------------------------------------------------

  async function refresh() {
    const s = await api(`/api/state?since=${midnight()}`);
    SM.showClock(s.clock);
    tariff = s.tariff;
    renderModes();
    reconcile(s.devices);
    renderTotals();
  }

  async function tick() {
    if (document.hidden) return;
    try { await refresh(); await loadCharts(); } catch (e) { /* the banner reports it */ }
  }

  stream({
    live(ev) {
      const dev = devices.get(ev.id);
      if (!dev) return;
      dev.live = ev;
      updateRow(dev);
      renderTotals();
    },
    devices() { tick(); },
    clock() { initChrome("dashboard"); },
  }, (ok) => { $("#conn-banner").hidden = ok; });

  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    resetCharts();
    redrawFromCache();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });

  renderRanges();
  renderModes();
  initChrome("dashboard");
  tick();
  setInterval(tick, 30000);
})();
