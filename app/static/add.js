"use strict";
(() => {
  const { $, el, api, toast, initChrome } = SM;

  let phase = "scan"; // scan -> name -> setup
  let selected = null;
  let scanStarted = Date.now();
  let manual = null; // a candidate found by address
  let lastScan = [];

  const setStep = (n, state) => {
    const s = $(`#step-${n}`);
    s.classList.remove("is-active", "is-done", "is-future");
    s.classList.add(`is-${state}`);
  };

  // ---- step 1: join the plug's own Wi-Fi ------------------------------------------------

  const WORDS = { excellent: "excellent signal", good: "good signal", fair: "fair signal", weak: "weak signal" };
  let shown = { ssid: null, qrFor: null };  // which network the code is for
  let chosenSsid = null;                    // set when the person picks one from the list
  let wifiNets = [];
  let wifiError = null;
  let wifiStarted = Date.now();

  const label = (n) => `${n.ssid} · ${WORDS[n.quality] || ""}${n.known ? ` · already added as ${n.known_name}` : ""}`;

  function renderJoin() {
    const status = $("#join-text"), spin = $("#join-status .spinner");
    const pickBox = $("#join-pick"), qrRow = $("#join-qr-row"), help = $("#join-help");
    const nets = wifiNets;
    help.hidden = true;
    if (!nets.length) {
      pickBox.hidden = true; qrRow.hidden = true; shown = { ssid: null, qrFor: null };
      spin.hidden = !!wifiError;
      if (wifiError) {
        status.textContent = wifiError;
        help.textContent = "You can still join the plug's network by hand (see below) and carry on with the next step.";
        help.hidden = false;
      } else {
        status.textContent = "Looking for the plug's Wi-Fi…";
        if (Date.now() - wifiStarted > 45000) {
          help.textContent = "Nothing yet. Check the plug is plugged in and has been on for a minute. A plug that already joined a Wi-Fi network no longer shows its own; hold its button for about 10 seconds to reset it.";
          help.hidden = false;
        }
      }
      return;
    }
    spin.hidden = true;
    // the strongest is the default; a choice the person made stays while that network is still there
    const picked = nets.find((n) => n.ssid === chosenSsid) || nets[0];
    if (chosenSsid && picked.ssid !== chosenSsid) chosenSsid = null;
    if (nets.length === 1) {
      status.textContent = `Found the plug's Wi-Fi: ${picked.ssid}.`;
      pickBox.hidden = true;
    } else {
      status.textContent = `Found ${nets.length} plug networks. ${picked === nets[0] ? "The strongest is selected" : "You chose one"}; if it isn't the plug you're setting up, choose another:`;
      $("#join-pick-label").textContent = `${nets.length} plug networks in range`;
      const sel = $("#join-select");
      sel.replaceChildren(...nets.map((n, i) => el("option", { value: n.ssid, text: label(n) + (i === 0 ? " · strongest" : "") })));
      sel.value = picked.ssid;
      pickBox.hidden = false;
    }
    shown.ssid = picked.ssid;
    $("#join-ssid").textContent = picked.ssid;
    $("#join-signal").textContent = `The Pi hears it with ${WORDS[picked.quality] || "some signal"}${picked.known ? `. This one is already on your dashboard as ${picked.known_name}.` : "."}`;
    if (picked.secured) {
      qrRow.hidden = true;
      help.textContent = `${picked.ssid} is protected by a password, so a code can't join it. Join it by hand with the password the plug was given.`;
      help.hidden = false;
    } else {
      qrRow.hidden = false;
      if (shown.qrFor !== picked.ssid) {
        shown.qrFor = picked.ssid;
        $("#qr-join").src = `/api/wizard/qr.svg?kind=plugwifi&ssid=${encodeURIComponent(picked.ssid)}`;
      }
    }
  }

  $("#join-select").addEventListener("change", (e) => { chosenSsid = e.target.value; renderJoin(); });

  let wifiWake = null;   // lets the loop skip its wait, e.g. when the tab comes back to the front
  async function wifiLoop() {
    let first = true;
    while (phase === "scan") {
      // Always ask once at the start. After that, skip rounds while the tab is hidden, so the Pi
      // isn't scanned for nobody; coming back to the tab asks again straight away.
      if (first || !document.hidden) {
        first = false;
        try {
          wifiNets = (await api("/api/wizard/plug-wifi")).networks;
          wifiError = null;
        } catch (e) { wifiError = e.message; }
        if (phase === "scan") renderJoin();
      }
      await new Promise((r) => { wifiWake = r; setTimeout(r, 8000); });
      wifiWake = null;
    }
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && wifiWake) wifiWake(); });

  // ---- step 2: what to send ------------------------------------------------------

  async function loadProvision() {
    try {
      const p = await api("/api/wizard/provision");
      $("#ssid-name").textContent = p.ssid;
      $("#fb-ssid").value = p.ssid;
      $("#fb-pass").value = p.password;
      $("#provision-link").href = p.provision_url;
      $("#plug-page-link").href = `http://${p.plug_ap_ip}/`;
      $("#qr-provision").src = "/api/wizard/qr.svg?kind=provision&t=" + Date.now();
      $("#qr-reboot").src = "/api/wizard/qr.svg?kind=reboot&t=" + Date.now();
    } catch (e) {
      toast(e.message);
    }
  }

  function copyFrom(input) {
    const wasPassword = input.type === "password";
    if (wasPassword) input.type = "text";
    input.select();
    input.setSelectionRange(0, input.value.length);
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { /* fall through */ }
    if (wasPassword) input.type = "password";
    input.blur();
    toast(ok ? "Copied." : "Couldn't copy. Select the text and copy it by hand.");
  }
  document.querySelectorAll("[data-copy]").forEach((b) =>
    b.addEventListener("click", () => copyFrom($(`#${b.dataset.copy}`))));
  $("#fb-pass-show").addEventListener("click", (e) => {
    const inp = $("#fb-pass");
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    e.target.textContent = show ? "Hide" : "Show";
  });

  // ---- step 2: find the plug ------------------------------------------------------

  function renderCandidates() {
    const list = [...lastScan];
    if (manual && !list.some((c) => c.mac === manual.mac)) list.push(manual);
    const box = $("#cands");
    box.replaceChildren(...list.map((c) => {
      const title = `${c.model || "Shelly"} ${c.mac ? c.mac.slice(-5).toUpperCase() : ""}`.trim();
      let sub = `Address ${c.host}`;
      if (c.known) sub += `, already added as ${c.known_name}`;
      if (!c.supported) sub = c.problem;
      return el("div", { class: "cand" + (c.supported ? "" : " is-bad") },
        el("div", {}, el("div", { class: "t", text: title }), el("div", { class: "s", text: sub })),
        c.supported ? el("button", {
          type: "button", class: "btn primary", text: c.known ? "Set up again" : "Use this plug",
          onclick: () => choose(c),
        }) : null);
    }));
    const found = list.length > 0;
    $("#scan-text").textContent = found
      ? "Found a plug. Pick it to continue."
      : "Looking for new plugs on the Pi's Wi-Fi…";
    $("#scan-status .spinner").hidden = found;
    $("#trouble").hidden = found || Date.now() - scanStarted < 45000;
  }

  async function scanLoop() {
    while (phase === "scan") {
      try {
        lastScan = (await api("/api/wizard/scan")).candidates;
        if (phase === "scan") renderCandidates();
      } catch (e) { /* keep trying; the Pi may be busy */ }
      await new Promise((r) => setTimeout(r, 2500));
    }
  }

  $("#manual-find").addEventListener("click", async () => {
    const host = $("#manual-host").value.trim();
    const msg = $("#manual-msg");
    msg.textContent = "";
    if (!host) { msg.textContent = "Enter the plug's address first."; return; }
    try {
      manual = await api(`/api/wizard/probe?host=${encodeURIComponent(host)}`);
      renderCandidates();
    } catch (e) { msg.textContent = e.message; }
  });

  // ---- step 3: name it ---------------------------------------------------------------

  function choose(c) {
    selected = c;
    phase = "name";
    setStep("join", "done"); setStep(1, "done"); setStep(2, "done"); setStep(3, "active"); setStep(4, "future");
    $("#step-3-body").hidden = false;
    $("#step-4-body").hidden = true;
    $("#chosen").textContent = `${c.model || "Shelly"} at ${c.host}`;
    if (c.known && !$("#f-name").value) $("#f-name").value = c.known_name || "";
    $("#f-name").focus();
    $("#step-3").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  $("#f-back").addEventListener("click", () => {
    phase = "scan"; selected = null; scanStarted = Date.now();
    setStep("join", "active"); setStep(1, "active"); setStep(2, "active"); setStep(3, "future"); setStep(4, "future");
    $("#step-3-body").hidden = true;
    renderCandidates();
    scanLoop();
    wifiLoop();
  });

  $("#name-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const name = $("#f-name").value.trim();
    $("#f-name-err").hidden = !!name;
    if (!name) { $("#f-name").focus(); return; }
    $("#f-submit").disabled = true;
    try {
      const job = await api("/api/wizard/adopt", {
        method: "POST",
        body: {
          host: selected.host, name, room: $("#f-room").value.trim(),
          initial_state: document.querySelector('input[name="initial"]:checked').value,
          disable_ap: $("#f-ap").checked,
        },
      });
      startProgress(job, name);
    } catch (e) {
      toast(e.message);
      $("#f-submit").disabled = false;
    }
  });

  // ---- step 4: progress -----------------------------------------------------------------

  const MARKS = { ok: "✓", warn: "!", failed: "×", running: "", pending: "" };

  function renderJob(job) {
    $("#progress").replaceChildren(...job.steps.map((s) =>
      el("li", { class: `st-${s.status}` },
        el("span", { class: "mark", "aria-hidden": "true", text: MARKS[s.status] || "" }),
        el("div", {}, el("div", { text: s.label }),
          s.detail ? el("div", { class: "detail", text: s.detail }) : null))));
  }

  async function startProgress(job, name) {
    phase = "setup";
    setStep(3, "done"); setStep(4, "active");
    $("#step-4-body").hidden = false;
    $("#result").hidden = true;
    $("#step-4").scrollIntoView({ behavior: "smooth", block: "start" });
    while (job.status === "running") {
      renderJob(job);
      await new Promise((r) => setTimeout(r, 800));
      try { job = await api(`/api/wizard/jobs/${job.id}`); } catch (e) { /* retry */ }
    }
    renderJob(job);
    const result = $("#result");
    result.hidden = false;
    if (job.status === "done") {
      setStep(4, "done");
      result.className = "result ok";
      result.replaceChildren(
        el("h3", { text: `${name} is on the dashboard` }),
        el("p", { text: "You can switch it on or off there and see its power over time." }),
        el("div", { class: "actions" },
          el("a", { class: "btn primary", href: "/", text: "Go to the dashboard" }),
          el("a", { class: "btn", href: "/add", text: "Add another plug" })));
    } else {
      result.className = "result bad";
      result.replaceChildren(
        el("h3", { text: "That didn't finish" }),
        el("p", { text: job.error || "Something went wrong." }),
        el("div", { class: "actions" },
          el("button", { type: "button", class: "btn primary", text: "Try again", onclick: () => {
            $("#f-submit").disabled = false;
            phase = "name";
            setStep(3, "active"); setStep(4, "future");
            $("#step-4-body").hidden = true;
            $("#step-3").scrollIntoView({ behavior: "smooth", block: "start" });
          } })));
    }
  }

  // ---- go ---------------------------------------------------------------------------------

  initChrome("add");
  loadProvision();
  scanLoop();
  wifiLoop();
})();
