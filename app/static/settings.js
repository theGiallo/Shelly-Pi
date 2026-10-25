"use strict";
(() => {
  const { $, el, api, toast, fmt, initChrome } = SM;

  let sys = null;
  let skew = 0; // Pi time minus this device's time, in seconds, at the last fetch

  function renderClock() {
    const now = Date.now() / 1000;
    const pi = now + skew;
    $("#pi-time").textContent = fmt.clock(pi);
    $("#my-time").textContent = fmt.clock(now);
    const d = Math.round(skew);
    $("#diff").textContent = Math.abs(d) < 2 ? "In sync"
      : `The Pi is ${fmt.age(Math.abs(d))} ${d > 0 ? "ahead of" : "behind"} this device`;
  }

  function render() {
    const c = sys.clock;
    skew = c.now - Date.now() / 1000;
    $("#clock-warn").hidden = c.trusted;
    if (!c.trusted) {
      $("#clock-warn-text").textContent = " " + (SM.CLOCK_REASON[c.reason] || "");
      $("#held").textContent = String(sys.held_samples);
    }
    renderClock();
    $("#hw-clock").textContent = sys.hw_clock && sys.hw_clock.present
      ? `Found (${sys.hw_clock.name}). The Pi keeps the time through power cuts.`
      : "None found. Connect a DS3231 module with the Pi powered off; it is found by itself at the next start. If it still isn't, run the installer again so the Pi switches the module's support on.";

    $("#ap-ssid").textContent = sys.ap.ssid;
    $("#ap-ip").textContent = `http://${sys.ap.ip}`;
    $("#ap-pass").dataset.value = sys.ap.password || "";
    if (!sys.ap.password) { $("#ap-pass").textContent = "No password set"; $("#ap-show").hidden = true; }

    $("#up-version").textContent = sys.version;
    $("#ab-version").textContent = sys.version;
    $("#qr-apwifi").src = "/api/wizard/qr.svg?kind=apwifi&t=" + Date.now();
    $("#qr-dash").src = "/api/wizard/qr.svg?kind=dashboard";
    $("#qr-login-row").hidden = !sys.auth;

    const db = sys.db;
    const rows = [
      ["Plugs", String(db.devices)],
      ["Database size", fmt.bytes(db.bytes)],
      ["Recent readings", db.readings.toLocaleString()],
      ["Minute averages", db.rollup_1m.toLocaleString()],
      ["15-minute averages", db.rollup_15m.toLocaleString()],
      ["Waiting to be saved", String(sys.held_samples)],
    ];
    $("#data-kv").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
    const lm = sys.last_maintenance && sys.last_maintenance.at;
    $("#retention").textContent =
      `Readings are saved every ${Math.round(sys.flush_interval / 60)} minutes. ` +
      `Full detail is kept for ${sys.retention.raw_days} days, minute averages for ${sys.retention.r1m_days} days, ` +
      `and 15-minute averages for good.` + (lm ? ` Last tidy-up: ${fmt.clock(lm)}.` : "");
    if (sys.last_flush_error) $("#retention").textContent += ` Last save failed: ${sys.last_flush_error}`;
    $("#version").textContent = sys.version;
  }

  async function load() {
    try {
      sys = await api("/api/system");
      render();
      SM.showClock(sys.clock, sys.held_samples);
    } catch (e) { toast(e.message); }
  }

  $("#qr-login").addEventListener("change", (e) => {
    $("#qr-dash").src = "/api/wizard/qr.svg?kind=dashboard" + (e.target.checked ? "&login=1" : "");
    $("#qr-login-note").hidden = !e.target.checked;
  });

  $("#ap-show").addEventListener("click", (e) => {
    const p = $("#ap-pass");
    const shown = p.dataset.shown === "1";
    p.textContent = shown ? "•".repeat(8) : p.dataset.value;
    p.dataset.shown = shown ? "0" : "1";
    e.target.textContent = shown ? "Show" : "Hide";
  });

  $("#set-time").addEventListener("click", async (e) => {
    const btn = e.target;
    btn.disabled = true;
    btn.textContent = "Setting the clock…";
    try {
      await api("/api/system/time", { method: "POST", body: { epoch: Date.now() / 1000 } });
      toast("The Pi's clock is set.");
      await load();
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Set the Pi's clock from this device";
    }
  });

  // ---- electricity cost -----------------------------------------------------------------------

  const DAYS = [["Mon", "Monday"], ["Tue", "Tuesday"], ["Wed", "Wednesday"], ["Thu", "Thursday"],
    ["Fri", "Friday"], ["Sat", "Saturday"], ["Sun", "Sunday"]];
  let slots = []; // working copy of the slots being edited
  let tariff = null;

  const deviceZone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { return ""; } };

  function slotRow(slot, i) {
    const days = el("div", { class: "days", role: "group", "aria-label": "Days" }, DAYS.map(([short, long], d) =>
      el("button", {
        type: "button", class: "day", text: short.slice(0, 2), "aria-label": long, "aria-pressed": String(slot.days.includes(d)),
        onclick: (e) => {
          const on = e.currentTarget.getAttribute("aria-pressed") !== "true";
          e.currentTarget.setAttribute("aria-pressed", String(on));
          slot.days = on ? [...new Set([...slot.days, d])].sort() : slot.days.filter((x) => x !== d);
        },
      })));
    const field = (label, input) => el("label", { class: "mini" }, el("span", { text: label }), input);
    const bind = (key, input, conv = (v) => v) => { input.addEventListener("input", () => { slot[key] = conv(input.value); }); return input; };
    return el("div", { class: "slot" },
      el("div", { class: "slot-main" },
        field("Name", bind("name", el("input", { type: "text", maxlength: 24, value: slot.name, placeholder: "Night" }))),
        field("Price per kWh", bind("price", el("input", { type: "number", min: 0, step: "any", value: slot.price, inputmode: "decimal" }))),
        field("From", bind("start", el("input", { type: "time", step: 900, value: slot.start }))),
        field("To", bind("end", el("input", { type: "time", step: 900, value: slot.end }))),
      ),
      days,
      el("div", { class: "slot-tools" },
        el("button", { type: "button", class: "btn ghost small", text: "Move up", disabled: i === 0,
          onclick: () => { [slots[i - 1], slots[i]] = [slots[i], slots[i - 1]]; renderSlots(); } }),
        el("button", { type: "button", class: "btn ghost small", text: "Move down", disabled: i === slots.length - 1,
          onclick: () => { [slots[i + 1], slots[i]] = [slots[i], slots[i + 1]]; renderSlots(); } }),
        el("button", { type: "button", class: "btn ghost small danger", text: "Remove",
          onclick: () => { slots.splice(i, 1); renderSlots(); } }),
      ));
  }

  function renderSlots() {
    $("#t-slots").replaceChildren(...slots.map(slotRow));
    $("#t-add").disabled = slots.length >= 12;
  }

  /** The weekly strip comes from the saved prices, computed by the Pi, so it is what really applies. */
  function renderWeek(t) {
    const colour = new Map();
    const key = (i) => (i < 0 ? "base" : `${t.slots[i].name}|${t.slots[i].price}`);
    const tone = (i) => {
      if (i < 0) return "var(--track)";
      const k = key(i);
      if (!colour.has(k)) colour.set(k, colour.size);
      return `color-mix(in srgb, var(--accent) ${Math.max(28, 100 - colour.get(k) * 22)}%, var(--panel))`;
    };
    const label = (i) => (i < 0 ? `Base price, ${SM.fmt.price(t.base_price, t)}` : `${t.slots[i].name}, ${SM.fmt.price(t.slots[i].price, t)}`);
    const rows = [];
    t.week.forEach((cells, d) => {
      const strip = el("div", { class: "strip" });
      let a = 0;
      for (let q = 1; q <= cells.length; q++) {
        if (q === cells.length || cells[q] !== cells[a]) {
          const seg = el("span", { title: `${DAYS[d][0]} ${String(Math.floor(a / 4)).padStart(2, "0")}:${String((a % 4) * 15).padStart(2, "0")}\u2013${String(Math.floor(q / 4) % 24).padStart(2, "0")}:${String((q % 4) * 15).padStart(2, "0")}: ${label(cells[a])}` });
          seg.style.flexGrow = String(q - a);
          seg.style.background = tone(cells[a]);
          strip.append(seg);
          a = q;
        }
      }
      rows.push(el("div", { class: "week-row" }, el("span", { class: "week-day", text: DAYS[d][0] }), strip));
    });
    rows.push(el("div", { class: "week-row" }, el("span", { class: "week-day" }),
      el("div", { class: "ticks" }, ["0", "6", "12", "18", "24"].map((h) => el("span", { text: h })))));
    $("#t-week").replaceChildren(...rows);

    const seen = new Set(), items = [];
    for (let i = -1; i < t.slots.length; i++) {
      if (i >= 0 && !t.week.some((r) => r.includes(i))) continue; // fully covered by earlier slots
      const k = key(i);
      if (seen.has(k)) continue;
      seen.add(k);
      const sw = el("span", { class: "swatch" });
      sw.style.background = tone(i);
      items.push(el("li", {}, sw, label(i)));
    }
    $("#t-legend").replaceChildren(...items);
  }

  function showTariff(t) {
    tariff = t;
    $("#t-enabled").checked = t.enabled;
    $("#t-currency").value = t.currency;
    $("#t-position").value = t.currency_after ? "after" : "before";
    $("#t-decimals").value = String(t.decimals);
    $("#t-base").value = t.base_price || "";
    $("#t-tz").value = t.timezone === "UTC" && !t.enabled ? deviceZone() || "UTC" : t.timezone;
    slots = t.slots.map((s) => ({ ...s, days: [...s.days] }));
    renderSlots();
    renderWeek(t);
  }

  function collectTariff() {
    const num = (v) => (v === "" || v == null ? NaN : Number(v));
    return {
      enabled: $("#t-enabled").checked,
      currency: $("#t-currency").value.trim(),
      currency_after: $("#t-position").value === "after",
      decimals: Number($("#t-decimals").value),
      timezone: $("#t-tz").value.trim(),
      base_price: num($("#t-base").value) || 0,
      slots: slots.map((s) => ({ name: s.name.trim(), price: num(s.price), days: s.days, start: s.start, end: s.end })),
    };
  }

  async function loadTariff() {
    try { showTariff(await api("/api/tariff")); } catch (e) { toast(e.message); }
  }

  $("#t-add").addEventListener("click", () => {
    slots.push({ name: "", price: "", days: [0, 1, 2, 3, 4], start: "08:00", end: "19:00" });
    renderSlots();
    $("#t-slots").lastElementChild.querySelector("input").focus();
  });
  $("#t-tz-here").addEventListener("click", () => { const z = deviceZone(); if (z) $("#t-tz").value = z; });
  $("#t-enabled").addEventListener("change", () => { $("#t-body").classList.toggle("is-off", !$("#t-enabled").checked); });
  $("#t-save").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const body = collectTariff();
    if (body.slots.some((s) => Number.isNaN(s.price))) { toast("Every time slot needs a price."); return; }
    btn.disabled = true;
    try {
      showTariff(await api("/api/tariff", { method: "PUT", body }));
      toast(body.enabled ? "Prices saved. Costs are now shown on the dashboard." : "Prices saved. Costs are switched off.");
    } catch (err) { toast(err.message); }
    finally { btn.disabled = false; }
  });
  $("#t-enabled").addEventListener("change", () => $("#t-body").classList.toggle("is-off", !$("#t-enabled").checked));

  const zones = (Intl.supportedValuesOf && (() => { try { return Intl.supportedValuesOf("timeZone"); } catch (e) { return []; } })()) || [];
  $("#t-zones").replaceChildren(...zones.map((z) => el("option", { value: z })));

  // ---- HTTPS -----------------------------------------------------------------------------------------

  let https = null;

  function renderHttps(h) {
    https = h;
    const box = $("#https");
    box.hidden = !h.available;
    if (!h.available) return;
    const onHttps = location.protocol === "https:";
    const host = location.hostname;
    const portPart = h.port === 443 ? "" : `:${h.port}`;

    let status = "Off";
    if (h.listening) status = onHttps ? "On, and this page is using it" : "On";
    else if (h.enabled) status = h.error ? "Not running" : "Starting…";
    $("#https-status").textContent = status;
    const warn = $("#https-warn");
    warn.hidden = !h.error;
    warn.textContent = h.error || "";

    const addr = `https://${h.hostname}.local${portPart}/`;
    $("#https-addr").textContent = h.listening ? addr : "–";
    const srv = h.server;
    $("#https-cert").textContent = srv
      ? `valid until ${new Date(srv.not_after * 1000).toLocaleDateString([], { year: "numeric", month: "long", day: "numeric" })} (${Math.round(srv.days_left)} days)`
      : "none yet";
    $("#https-names").textContent = srv ? [...srv.dns, ...srv.ips].join(", ") : "–";
    $("#ca-fp").textContent = h.ca ? h.ca.fingerprint : "–";
    $("#ca-download").hidden = !h.ca;
    $("#https-enabled").checked = h.enabled;
    $("#https-only").checked = h.https_only;
    $("#https-only").disabled = !h.enabled || !h.listening;
    const open = $("#https-open");
    open.replaceChildren();
    if (h.listening && !onHttps) {
      const target = `https://${host}${portPart}/settings`;
      open.append("Open the dashboard over HTTPS: ", el("a", { href: target, text: target }),
        ". The address only works without a warning once this device trusts the authority.");
    }
  }

  async function loadHttps() {
    try { renderHttps(await api("/api/https")); } catch (e) { toast(e.message); }
  }

  async function saveHttps() {
    try {
      renderHttps(await api("/api/https", { method: "PUT", body: {
        enabled: $("#https-enabled").checked, https_only: $("#https-only").checked } }));
    } catch (e) { toast(e.message); await loadHttps(); }
  }

  $("#https-enabled").addEventListener("change", saveHttps);
  $("#https-only").addEventListener("change", () => {
    if ($("#https-only").checked && !confirm("Plain HTTP will stop working except for the health check and the certificate download. Have all your devices installed the certificate?")) {
      $("#https-only").checked = false;
      return;
    }
    saveHttps();
  });
  $("#ca-reset").addEventListener("click", async (e) => {
    const btn = e.currentTarget; // currentTarget is gone after the first await
    if (!confirm("Create a new certificate authority? Every device that trusts the current one will show a warning until you install the new one.")) return;
    btn.disabled = true;
    try {
      renderHttps(await api("/api/https/reset-ca", { method: "POST", body: { confirm: true } }));
      toast("New certificate authority created. Download and install it on your devices.");
    } catch (err) { toast(err.message); }
    finally { btn.disabled = false; }
  });

  // ---- backup and restore -------------------------------------------------------------------------

  // The file name's time comes from the Pi's clock shown in this device's time zone.
  $("#bk-download").href = `/api/backup?tz=${encodeURIComponent(deviceZone() || "UTC")}`;

  const bkFile = $("#bk-file");
  bkFile.addEventListener("change", () => {
    const f = bkFile.files[0];
    $("#bk-result").textContent = "";
    $("#bk-confirm").hidden = !f;
    if (f) $("#bk-chosen").textContent = `${f.name} (${fmt.bytes(f.size)})`;
  });

  $("#bk-restore").addEventListener("click", async (e) => {
    const f = bkFile.files[0];
    if (!f) return;
    const btn = e.currentTarget;
    btn.disabled = true;
    $("#bk-result").textContent = "Restoring. This can take a minute for a big backup…";
    try {
      const r = await api("/api/restore", { method: "POST", file: f, type: "application/zip" });
      const m = r.manifest || {};
      $("#bk-result").textContent = `Restored the backup from ${m.created_local || "an earlier time"}: ${m.counts ? m.counts.devices : "?"} plugs. Reloading…`;
      toast("The backup is restored.");
      setTimeout(() => location.reload(), 1800);
    } catch (err) {
      $("#bk-result").textContent = err.message;
      toast(err.message);
      btn.disabled = false;
    }
  });

  // ---- which Wi-Fi the plugs join -----------------------------------------------------------------

  let plugnet = null;
  const pnMode = () => ($("#pn-router").checked ? "router" : "pi");
  function renderPlugnet() {
    const p = plugnet;
    $("#pn-pi").checked = p.mode === "pi";
    $("#pn-router").checked = p.mode === "router";
    $("#pn-pi").disabled = !p.pi_wifi;
    $("#pn-pi-note").textContent = p.pi_wifi ? `(${p.pi_ssid})` : "(this Pi has no Wi-Fi)";
    if (!$("#pn-ssid").value) $("#pn-ssid").value = p.router_ssid || "";
    $("#pn-pass").placeholder = p.router_has_password ? "Saved. Type a new one to change it." : "Leave empty if the network has no password";
    $("#pn-router-box").hidden = pnMode() !== "router";
    $("#pi-wifi").hidden = p.mode !== "pi";
  }
  async function loadPlugnet() {
    try { plugnet = await api("/api/plugnet"); renderPlugnet(); } catch (e) { toast(e.message); }
  }
  document.querySelectorAll('input[name="plugnet"]').forEach((r) =>
    r.addEventListener("change", () => { $("#pn-router-box").hidden = pnMode() !== "router"; $("#pn-msg").textContent = ""; }));
  $("#pn-save").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    $("#pn-msg").textContent = "";
    try {
      const body = { mode: pnMode(), ssid: $("#pn-ssid").value.trim() };
      if ($("#pn-pass").value !== "" || !plugnet.router_has_password || body.ssid !== plugnet.router_ssid) body.password = $("#pn-pass").value;
      plugnet = await api("/api/plugnet", { method: "PUT", body });
      $("#pn-pass").value = "";
      renderPlugnet();
      $("#pn-msg").textContent = plugnet.mode === "router"
        ? "Saved. New plugs will be told to join this network. Plugs you already added keep working as they are."
        : "Saved. New plugs will join the Pi's own network.";
    } catch (err) { $("#pn-msg").textContent = err.message; }
    btn.disabled = false;
  });
  loadPlugnet();

  // ---- power --------------------------------------------------------------------------------------

  let pwAction = null;
  function askPower(action) {
    pwAction = action;
    const off = action === "poweroff";
    $("#pw-q").textContent = off ? "Shut down the Pi?" : "Restart the Pi?";
    $("#pw-msg").textContent = "";
    $("#pw-ask").textContent = off
      ? "The dashboard stops until someone plugs the Pi back in or presses its power button. No readings are taken while it is off."
      : "The dashboard is gone for a minute or two, then comes back by itself. No readings are taken meanwhile.";
    $("#pw-go").textContent = off ? "Shut down" : "Restart";
    $("#pw-confirm").hidden = false;
    $("#pw-go").focus();
  }
  $("#pw-off").addEventListener("click", () => askPower("poweroff"));
  $("#pw-reboot").addEventListener("click", () => askPower("reboot"));
  $("#pw-cancel").addEventListener("click", () => { $("#pw-confirm").hidden = true; pwAction = null; });
  $("#pw-go").addEventListener("click", async () => {
    if (!pwAction) return;
    const off = pwAction === "poweroff";
    $("#pw-confirm").hidden = true;
    try {
      await api("/api/power", { method: "POST", body: { action: pwAction, confirm: true } });
      $("#pw-off").disabled = $("#pw-reboot").disabled = true;
      $("#pw-msg").textContent = off
        ? "Shutting down. Wait about 20 seconds, until the green light on the Pi stops blinking, then it is safe to unplug."
        : "Restarting. This page comes back when the Pi is up again; reload it in a minute or two.";
    } catch (err) { $("#pw-msg").textContent = err.message; toast(err.message); }
  });

  // ---- about --------------------------------------------------------------------------------------

  $("#ab-project").href = "https://github.com/theGiallo/Shelly-Pi";
  $("#about-open").addEventListener("click", async () => {
    try {
      const a = await api("/api/about");
      $("#abd-version").textContent = a.version;
      $("#abd-project").href = a.project;
      $("#abd-changelog").textContent = a.changelog || "No changelog on this Pi.";
      $("#abd-license").textContent = a.license || "See the LICENSE file in the project.";
      $("#abd-third").textContent = a.third_party.map((t) => `${t.name}\n\n${t.license}`).join("\n\n");
      $("#about-dialog").showModal();
    } catch (err) { toast(err.message); }
  });

  // Close with the X, or by clicking outside the box (a click on the dimmed area lands on the dialog element itself).
  const aboutDlg = $("#about-dialog");
  $("#about-x").addEventListener("click", () => aboutDlg.close());
  aboutDlg.addEventListener("click", (e) => {
    if (e.target !== aboutDlg) return;
    const r = aboutDlg.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) aboutDlg.close();
  });

  // ---- updating the app ---------------------------------------------------------------------------

  const REPO = "theGiallo/Shelly-Pi";
  // Newer releases: shelly-pi-image_v0.1.7.tar.gz (about a third of the size; this page unpacks it before sending).
  // Older ones: shelly-pi-image_v0.1.6.tar, or just shelly-pi-image.tar.
  const isPacked = (a) => /^shelly-pi-image_v[0-9][0-9.]*\.tar\.gz$/.test(a.name);
  const isImage = (a) => isPacked(a) || /^shelly-pi-image(_v[0-9][0-9.]*)?\.tar$/.test(a.name);
  const UNPACKED_SUM = /Unpacked image SHA-256:\s*([0-9a-f]{64})/i;
  let release = null; // { tag, size, sha256 (of the .tar), packedSha256 (of the .tar.gz, if packed), packed, url }
  const upFile = $("#up-file"), upSha = $("#up-sha"), upSend = $("#up-send");
  const normVersion = (v) => String(v || "").trim().replace(/^v/i, "");
  const upReady = () => { upSend.disabled = !(upFile.files[0] && /^[0-9a-f]{64}$/.test(upSha.value.trim().toLowerCase())); };
  upFile.addEventListener("change", () => {
    const f = upFile.files[0];
    $("#up-result").textContent = f && release && release.packed && /\.gz$/i.test(f.name) && f.size !== release.size
      ? `This file is ${fmt.bytes(f.size)} but the release's is ${fmt.bytes(release.size)}. The download may be incomplete.` : "";
    upReady();
  });
  upSha.addEventListener("input", upReady);

  // Drag the downloaded file onto the box instead of using the file picker.
  const drop = $("#up-drop");
  for (const type of ["dragenter", "dragover"]) {
    drop.addEventListener(type, (e) => { e.preventDefault(); drop.classList.add("is-over"); });
  }
  drop.addEventListener("dragleave", (e) => { if (!drop.contains(e.relatedTarget)) drop.classList.remove("is-over"); });
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("is-over");
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) return;
    if (!/\.(tar|gz|tgz)$/i.test(f.name)) { $("#up-result").textContent = `${f.name} isn't an update file. It should end in .tar.gz or .tar.`; return; }
    upFile.files = e.dataTransfer.files;
    upFile.dispatchEvent(new Event("change"));
  });
  upFile.addEventListener("change", () => {
    const f = upFile.files[0];
    $("#up-drop-text").textContent = f ? `${f.name} (${fmt.bytes(f.size)}) — drop another file to replace it, or` : "Drop the file here, or";
  });

  $("#up-check").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    $("#up-found").textContent = "Asking GitHub…";
    try {
      let res;
      try { res = await fetch(`https://api.github.com/repos/${REPO}/releases?per_page=10`, { headers: { Accept: "application/vnd.github+json" } }); }
      catch (err) { throw new Error("Couldn't reach GitHub. This device needs internet for that (the Pi doesn't). You can also paste the checksum below and pick a file you already have."); }
      if (!res.ok) throw new Error(`GitHub answered ${res.status}.`);
      const list = await res.json();
      const rel = list.find((r) => !r.draft && (r.assets || []).some(isImage));
      if (!rel) throw new Error("No release with an app image was found.");
      const unpackedSum = UNPACKED_SUM.exec(rel.body || "");
      const packed = unpackedSum && rel.assets.find(isPacked);
      const a = packed || rel.assets.filter((x) => isImage(x) && !isPacked(x)).sort((x, y) => y.name.length - x.name.length)[0];
      if (!a) throw new Error("No release with an app image was found.");
      const digest = String(a.digest || "").replace(/^sha256:/, "");
      release = { tag: rel.tag_name, size: a.size, packed: !!packed, packedSha256: packed ? digest : "",
        sha256: packed ? unpackedSum[1].toLowerCase() : digest, url: a.browser_download_url };
      const same = normVersion(rel.tag_name) === normVersion(sys && sys.version);
      $("#up-found").textContent = same
        ? `You have the newest version (${rel.tag_name}). You can still install it again.`
        : `Version ${rel.tag_name} is available (${fmt.bytes(a.size)}).`;
      $("#up-download").href = release.url;
      upSha.value = release.sha256;
      $("#up-offer").hidden = false;
      upReady();
    } catch (err) {
      $("#up-found").textContent = err.message;
      $("#up-offer").hidden = false;
    } finally { btn.disabled = false; }
  });

  // The release's .tar.gz is a third of the size to download; the Pi is slow at unpacking, so this device does it.
  async function isGzip(file) {
    const b = new Uint8Array(await file.slice(0, 2).arrayBuffer());
    return b[0] === 0x1f && b[1] === 0x8b;
  }

  async function unpack(file) {
    if (!window.DecompressionStream) {
      throw new Error("This browser can't unpack the file. Use a recent Firefox, Chrome or Safari, or pick the plain .tar.");
    }
    $("#up-result").textContent = "Unpacking the file on this device…";
    if (release && release.packedSha256 && file.size === release.size && window.crypto && crypto.subtle) {
      const sum = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", await file.arrayBuffer())))
        .map((x) => x.toString(16).padStart(2, "0")).join("");
      if (sum !== release.packedSha256) throw new Error("The downloaded file is damaged (its checksum differs). Download it again.");
    }
    try {
      return await new Response(file.stream().pipeThrough(new DecompressionStream("gzip"))).blob();
    } catch (err) {
      throw new Error("The file couldn't be unpacked. It may be incomplete. Download it again.");
    }
  }

  function sendUpdate(file, sha) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/update?sha256=${encodeURIComponent(sha)}`);
      xhr.setRequestHeader("X-Requested-With", "shelly-pi");
      xhr.setRequestHeader("Content-Type", "application/x-tar");
      xhr.upload.onprogress = (ev) => {
        if (!ev.lengthComputable) return;
        $("#up-bar").value = (ev.loaded / ev.total) * 100;
        $("#up-result").textContent = `Sending to the Pi: ${fmt.bytes(ev.loaded)} of ${fmt.bytes(ev.total)}`;
      };
      xhr.onerror = () => reject(new Error("Can't reach the Pi. Check the network connection."));
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (err) { /* not JSON */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error((data && typeof data.detail === "string" && data.detail) || `Request failed (${xhr.status})`));
      };
      xhr.send(file);
    });
  }

  async function followUpdate(id) {
    const text = { loading: "Loading the new version…", restarting: "Starting the new version…" };
    for (let i = 0; i < 400; i++) {   // up to about 13 minutes
      await new Promise((r) => setTimeout(r, 2000));
      let info;
      try { info = await api("/api/update"); } catch (err) { $("#up-result").textContent = "Restarting…"; continue; }
      const st = info.status;
      if (!st || st.id !== id) { $("#up-result").textContent = "Waiting for the Pi to pick up the update…"; continue; }
      if (st.state === "done") {
        $("#up-result").textContent = `${st.message} Reloading…`;
        toast("The app is updated.");
        setTimeout(() => location.reload(), 1500);
        return;
      }
      if (st.state === "rolled_back" || st.state === "failed") throw new Error(st.message);
      $("#up-result").textContent = text[st.state] || st.message;
    }
    throw new Error("The Pi is taking very long. Reload this page in a few minutes.");
  }

  upSend.addEventListener("click", async () => {
    let f = upFile.files[0];
    if (!f) return;
    upSend.disabled = true;
    $("#up-bar").hidden = false;
    $("#up-bar").value = 0;
    try {
      if (await isGzip(f)) f = await unpack(f);
      const r = await sendUpdate(f, upSha.value.trim().toLowerCase());
      $("#up-result").textContent = "Sent. Installing…";
      await followUpdate(r.id);
    } catch (err) {
      $("#up-result").textContent = err.message;
      toast(err.message);
      $("#up-bar").hidden = true;
      upReady();
    }
  });

  // ---- automatic backups to a USB drive ---------------------------------------------------------

  let ab = null;       // last status from the Pi
  let drives = [];     // drives plugged in right now
  let abDirty = false; // the person changed something we haven't saved

  const EVERY = { 1: "hour", 2: "2 hours", 3: "3 hours", 4: "4 hours", 6: "6 hours", 8: "8 hours", 12: "12 hours", 24: "24 hours (daily)", 48: "48 hours", 72: "72 hours", 168: "7 days (weekly)" };
  $("#ab-every").replaceChildren(...Object.entries(EVERY).map(([h, t]) => el("option", { value: h, text: h === "1" ? "Every hour" : `Every ${t}` })));
  $("#ab-hour").replaceChildren(...Array.from({ length: 24 }, (_, h) => el("option", { value: String(h), text: `${h}:00` })));

  // Drives are sold in decimal units (a "64 GB" stick), unlike file sizes.
  const driveSize = (n) => (n >= 1e12 ? `${(n / 1e12).toFixed(1)} TB` : n >= 1e9 ? `${Math.round(n / 1e9)} GB` : `${Math.round(n / 1e6)} MB`);
  const driveText = (d) => {
    const bits = [d.label || d.name || d.device, d.name && d.label ? d.name : "", driveSize(d.size), d.fstype].filter(Boolean);
    return bits.join(" · ") + (d.supported ? "" : " (can't be used)");
  };

  function renderDrives() {
    const sel = $("#ab-drive");
    const keep = abDirty ? sel.value : (ab && ab.plan.drive ? ab.plan.drive.id : sel.value);
    const opts = [el("option", { value: "", text: drives.length ? "Choose a drive…" : "No USB drive found" })];
    for (const d of drives) opts.push(el("option", { value: d.id, text: driveText(d), disabled: !d.supported }));
    const saved = ab && ab.plan.drive;
    if (saved && !drives.some((d) => d.id === saved.id)) {
      opts.push(el("option", { value: saved.id, text: `${driveText({ ...saved, supported: true })} (not plugged in)` }));
    }
    sel.replaceChildren(...opts);
    sel.value = [...sel.options].some((o) => o.value === keep) ? keep : "";
  }

  function when(epoch) {
    return new Date(epoch * 1000).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  function renderBackupPlan(s) {
    ab = s;
    const p = s.plan;
    if (!abDirty) {
      $("#ab-enabled").checked = p.enabled;
      $("#ab-every").value = String(p.every_hours);
      $("#ab-hour").value = String(p.hour);
      $("#ab-keep").value = String(p.keep);
      $("#ab-mode").value = p.thin ? "thin" : "last";
      $("#ab-tz").value = p.timezone === "UTC" && !p.enabled && !p.drive ? deviceZone() || "UTC" : p.timezone;
    }
    $("#ab-body").classList.toggle("is-off", !$("#ab-enabled").checked);
    $("#ab-keep-field").hidden = $("#ab-mode").value === "thin";
    $("#ab-thin-hint").hidden = $("#ab-mode").value !== "thin";
    $("#ab-hour-label").textContent = Number($("#ab-every").value) >= 24 ? "Time of day" : "First one at";
    renderDrives();

    let next = "–";
    if (!s.clock_trusted) next = "Waiting for the Pi's clock to be set";
    else if (p.enabled && p.drive && s.next) next = s.next <= s.now ? "As soon as possible" : when(s.next);
    else if (!p.enabled) next = "Switched off";
    $("#ab-next").textContent = s.running ? "Running now…" : next;

    const l = s.last;
    const last = $("#ab-last");
    if (!l) last.textContent = "None yet";
    else if (l.ok) last.textContent = `${when(l.time)}: saved ${l.file} (${fmt.bytes(l.bytes)}) to ${l.drive}. ${fmt.bytes(l.free)} free on the drive.`;
    else last.textContent = `${when(l.time)}: failed. ${l.message}`;
    last.classList.toggle("bad", !!l && !l.ok);
    $("#ab-run").disabled = s.running;
  }

  async function loadBackupPlan() {
    try {
      renderBackupPlan(await api("/api/autobackup"));
    } catch (e) { toast(e.message); }
  }

  async function lookForDrives(btn) {
    if (btn) btn.disabled = true;
    $("#ab-drive-hint").textContent = "Looking for drives…";
    try {
      drives = (await api("/api/autobackup/drives")).drives;
      $("#ab-drive-hint").textContent = drives.length
        ? "Only drives plugged in right now are listed. Needs exFAT, FAT32, NTFS or ext4."
        : "No USB drive found. Plug one into the Pi and choose Look again.";
    } catch (e) {
      $("#ab-drive-hint").textContent = e.message;
    } finally {
      if (btn) btn.disabled = false;
    }
    if (ab) renderDrives();
  }

  function collectBackupPlan() {
    return {
      enabled: $("#ab-enabled").checked,
      drive_id: $("#ab-drive").value || null,
      every_hours: Number($("#ab-every").value),
      hour: Number($("#ab-hour").value),
      keep: Number($("#ab-keep").value) || 14,
      thin: $("#ab-mode").value === "thin",
      timezone: $("#ab-tz").value.trim() || "UTC",
    };
  }

  for (const id of ["#ab-enabled", "#ab-drive", "#ab-every", "#ab-hour", "#ab-keep", "#ab-mode", "#ab-tz"]) {
    $(id).addEventListener("change", () => {
      abDirty = true;
      $("#ab-body").classList.toggle("is-off", !$("#ab-enabled").checked);
      $("#ab-hour-label").textContent = Number($("#ab-every").value) >= 24 ? "Time of day" : "First one at";
      $("#ab-keep-field").hidden = $("#ab-mode").value === "thin";
      $("#ab-thin-hint").hidden = $("#ab-mode").value !== "thin";
    });
  }
  $("#ab-refresh").addEventListener("click", (e) => lookForDrives(e.currentTarget));
  $("#ab-tz-here").addEventListener("click", () => { const z = deviceZone(); if (z) { $("#ab-tz").value = z; abDirty = true; } });

  $("#ab-save").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const body = collectBackupPlan();
    if (body.enabled && !body.drive_id) { toast("Choose a drive first."); return; }
    btn.disabled = true;
    try {
      const s = await api("/api/autobackup", { method: "PUT", body });
      abDirty = false;
      renderBackupPlan(s);
      toast(body.enabled ? "Saved. Backups to the drive are on." : "Saved. Automatic backups are off.");
    } catch (err) { toast(err.message); }
    finally { btn.disabled = false; }
  });

  $("#ab-run").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (abDirty && !confirm("Back up now using the drive saved earlier? Your changes on this page aren't saved yet.")) return;
    btn.disabled = true;
    $("#ab-msg").textContent = "Backing up to the drive. This can take a minute…";
    try {
      const s = await api("/api/autobackup/run", { method: "POST", body: {} });
      renderBackupPlan(s);
      $("#ab-msg").textContent = s.result.ok ? "Done. You can unplug the drive now." : s.result.message;
    } catch (err) { $("#ab-msg").textContent = err.message; toast(err.message); }
    finally { btn.disabled = false; }
  });

  SM.stream({ clock() { load(); } });
  initChrome("settings");
  load();
  loadHttps();
  setInterval(loadHttps, 30000);
  loadBackupPlan().then(() => lookForDrives());
  setInterval(loadBackupPlan, 30000);
  loadTariff().then(() => $("#t-body").classList.toggle("is-off", !$("#t-enabled").checked));
  setInterval(renderClock, 1000);
  setInterval(load, 60000);
})();
