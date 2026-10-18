/* Shared helpers for all pages. Plain JS, no build step, no network beyond this server. */
"use strict";

const SM = (() => {
  const $ = (sel, root = document) => root.querySelector(sel);

  /** Build an element. Text goes in as text (never HTML), so plug names can't inject markup. */
  function el(tag, attrs = {}, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === false || v == null) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }

  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: { "X-Requested-With": "shelly-monitor" } };
    if (opts.file !== undefined) {
      init.headers["Content-Type"] = opts.type || "application/octet-stream";
      init.body = opts.file; // sent as is, not wrapped in JSON
    } else if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      throw new Error("Can't reach the Pi. Check the network connection.");
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) {
      const detail = data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
      const err = new Error(detail || `Request failed (${res.status})`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  let toastTimer = null;
  function toast(message) {
    let t = $("#toast");
    if (!t) {
      t = el("div", { id: "toast", class: "toast", role: "status" });
      document.body.append(t);
    }
    t.textContent = message;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
  }

  const fmt = {
    watts(v) {
      if (v == null) return "–";
      return v >= 100 ? String(Math.round(v)) : v.toFixed(1);
    },
    energy(wh) {
      if (wh == null) return "–";
      return wh < 1000 ? `${Math.round(wh)} Wh` : `${(wh / 1000).toFixed(2)} kWh`;
    },
    /** An amount of money in the person's currency, e.g. "0,31 €" or "£0.31". */
    money(v, t) {
      if (v == null || !t) return "–";
      const d = t.decimals;
      const n = v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
      return t.currency_after ? `${n}\u00a0${t.currency}` : `${t.currency}\u00a0${n}`;
    },
    /** A price per kWh: keeps up to 4 decimals because kWh prices are small numbers. */
    price(v, t) {
      if (v == null || !t) return "–";
      const n = v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 });
      return (t.currency_after ? `${n}\u00a0${t.currency}` : `${t.currency}\u00a0${n}`) + "/kWh";
    },
    /** Energy as a bare number in a chosen unit ("Wh" or "kWh"). */
    energyIn(wh, unit) {
      return unit === "kWh" ? wh / 1000 : wh;
    },
    age(seconds) {
      if (seconds == null) return "never";
      if (seconds < 90) return `${Math.round(seconds)} s`;
      if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
      return `${Math.round(seconds / 3600)} h`;
    },
    clock(ts) {
      return new Date(ts * 1000).toLocaleString([], {
        year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
      });
    },
    bytes(n) {
      if (n < 1024) return `${n} B`;
      if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
      if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
      return `${(n / 1073741824).toFixed(1)} GB`;
    },
  };

  const store = {
    get(key, fallback) {
      try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode */ }
    },
  };

  const CLOCK_REASON = {
    before_minimum: "The Pi's clock is set to a date before 2026. It hasn't been set since the clock battery last ran out or the Pi was first set up.",
    went_backwards: "The Pi's clock is earlier than the newest reading it has saved.",
  };

  /** Header: highlight the current page, show a warning chip if the clock can't be trusted. */
  async function initChrome(current) {
    const nav = $(".nav");
    if (nav) for (const a of nav.querySelectorAll("a")) {
      if (a.dataset.page === current) a.setAttribute("aria-current", "page");
    }
    try {
      const sys = await api("/api/system");
      showClock(sys.clock, sys.held_samples);
    } catch (e) { /* the page itself reports connection problems */ }
  }

  function showClock(clock, held) {
    const slot = $("#clock-chip");
    if (!slot) return;
    slot.replaceChildren();
    if (clock && !clock.trusted) {
      slot.append(el("a", { class: "chip", href: "/settings#clock" },
        "Clock not set: readings are on hold",
        held ? ` (${held} waiting)` : ""));
    }
  }

  /** Read-only live feed. Calls handlers by event name; returns the EventSource. */
  function stream(handlers, onState) {
    const es = new EventSource("/api/stream");
    for (const [name, fn] of Object.entries(handlers)) {
      es.addEventListener(name, (e) => { try { fn(JSON.parse(e.data)); } catch (err) { console.error(err); } });
    }
    es.onopen = () => onState && onState(true);
    es.onerror = () => onState && onState(false);
    return es;
  }

  return { $, el, api, toast, fmt, store, initChrome, showClock, stream, CLOCK_REASON };
})();
