/*
 * Fake machine reader: behaves like the planned new firmware, talking to BBMS through the v2 device API
 * (docs/spec/09-device-api-v2-build.md). Plain JavaScript, no build step.
 *
 * What's "hardware" here:
 *   - a 296 x 128 one-bit e-paper screen (a canvas; it keeps its last image when the power is cut);
 *   - a relay (and its LED) that powers the machine while a session is open;
 *   - a fob reader (the text box and Tap), a red Stop button and a blue Fault button;
 *   - a current sensor on the machine's supply (the slider; it reads 0 while the relay is off).
 * "Flash memory" (kept through power cuts) is localStorage: the API key, the meter sequence number, meter
 * readings not yet sent, and the QR code. Everything else is lost when the power goes, like a real reboot.
 */
"use strict";

(() => {
  const API = "/api/devices/v2/";
  const FIRMWARE_VERSION = "fake-0.1";
  const METER_REPORT_SECONDS = 60;
  const RETRY_SECONDS = 15;
  const MESSAGE_SECONDS = 4;

  const $ = (id) => document.getElementById(id);
  const screen = $("screen");
  const ctx = screen.getContext("2d");

  // ---- Flash memory -----------------------------------------------------------------------------------------
  const flash = {
    get(name, fallback) {
      try {
        const value = localStorage.getItem("fakeDevice." + name);
        return value === null ? fallback : JSON.parse(value);
      } catch {
        return fallback;
      }
    },
    set(name, value) {
      try {
        localStorage.setItem("fakeDevice." + name, JSON.stringify(value));
      } catch {
        /* private window: flash just doesn't persist */
      }
    },
  };

  // ---- RAM: everything here is lost when the power goes ------------------------------------------------------
  let ram = null;
  let powered = false;
  let tickTimer = null;

  function freshRam() {
    return {
      generation: (ram ? ram.generation : 0) + 1, // replies from before a power cut are ignored
      booting: true,
      online: false,
      disabled: false,
      machine: null,
      heartbeatSeconds: 300,
      sessionHeartbeatSeconds: 60,
      nextCheckIn: Infinity,
      session: null, // the session this reader started (or resumed)
      orphan: null, // an open session BBMS knows about but this reader doesn't (after a power cut)
      pendingEnd: null, // an end BBMS hasn't confirmed yet: {id, body}
      nextSessionHeartbeat: Infinity,
      nextEndRetry: Infinity,
      activeSeconds: 0, // motor-on seconds since the last meter reading was queued
      nextMeterQueue: Date.now() + METER_REPORT_SECONDS * 1000,
      nextMeterSend: 0,
      message: null, // {lines, until}
      qr: null, // Uint8Array in the raw format
    };
  }

  // ---- Talking to BBMS ----------------------------------------------------------------------------------------
  // Returns null if the power was cut while waiting (the reply is ignored), otherwise
  // {ok, status, data, response}. ok means "200 with a body": anything else means BBMS couldn't decide.
  async function api(method, path, body, { binary = false, headers = {} } = {}) {
    const generation = ram.generation;
    const key = $("key").value.trim();
    const entry = logRequest(method, path, body);
    let result;
    if (!$("network").checked) {
      result = { ok: false, status: 0, data: null, response: null, error: "network unreachable (bench switch)" };
    } else {
      try {
        const response = await fetch(API + path, {
          method,
          headers: {
            Authorization: "Bearer " + key,
            ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
            ...headers,
          },
          body: body !== undefined ? JSON.stringify(body) : undefined,
          cache: "no-store",
        });
        let data = null;
        if (binary && response.status === 200) {
          data = new Uint8Array(await response.arrayBuffer());
        } else if ((response.headers.get("Content-Type") || "").includes("json")) {
          data = await response.json().catch(() => null);
        }
        result = { ok: response.status === 200 && data !== null, status: response.status, data, response };
      } catch (error) {
        result = { ok: false, status: 0, data: null, response: null, error: String(error) };
      }
    }
    logResponse(entry, result);
    if (!powered || !ram || ram.generation !== generation) return null;
    ram.online = result.ok || result.status === 304;
    return result;
  }

  // ---- Request log -------------------------------------------------------------------------------------------
  function logRequest(method, path, body) {
    const li = document.createElement("li");
    const time = new Date().toLocaleTimeString("en-GB");
    li.textContent = `${time}  ${method} ${path}`;
    if (body !== undefined) li.appendChild(pre("→ " + JSON.stringify(body)));
    $("log").prepend(li);
    while ($("log").children.length > 150) $("log").lastChild.remove();
    return li;
  }

  function logResponse(li, result) {
    let text = "← " + (result.status || "no response");
    if (result.error) text += "  " + result.error;
    if (result.data instanceof Uint8Array) text += `  (${result.data.length} bytes)`;
    else if (result.data) text += "  " + JSON.stringify(result.data, null, 1).replace(/\n\s*/g, " ");
    const line = pre(text);
    if (!result.ok && result.status !== 304) line.classList.add("bad");
    li.appendChild(line);
  }

  function pre(text) {
    const element = document.createElement("pre");
    element.textContent = text;
    return element;
  }

  // ---- Start-up, check-in and the QR code ---------------------------------------------------------------------
  async function powerOn() {
    powered = true;
    $("power").checked = true;
    ram = freshRam();
    document.querySelector(".device").classList.remove("unpowered");
    updateRelay();
    draw();
    clearInterval(tickTimer);
    tickTimer = setInterval(tick, 1000);
    if (!$("key").value.trim()) {
      ram.booting = false;
      showMessage(["No API key", "Paste one on the bench"], 3600);
      return;
    }
    await loadQr();
    await checkIn("boot");
    if (!ram) return;
    ram.booting = false;
    draw();
  }

  function powerOff() {
    powered = false;
    $("power").checked = false;
    clearInterval(tickTimer);
    tickTimer = null;
    if (ram) ram.generation += 1; // drop replies still on their way
    updateRelay();
    document.querySelector(".device").classList.add("unpowered"); // the e-paper keeps showing its last image
  }

  async function reboot() {
    powerOff();
    await new Promise((resolve) => setTimeout(resolve, 800));
    powerOn();
  }

  async function loadQr() {
    const stored = flash.get("qr", null); // {etag, bytes: [..]}
    const headers = stored ? { "If-None-Match": stored.etag } : {};
    const result = await api("GET", "qr", undefined, { binary: true, headers });
    if (!result) return;
    if (result.status === 200 && result.data) {
      flash.set("qr", { etag: result.response.headers.get("ETag"), bytes: Array.from(result.data) });
      ram.qr = result.data;
    } else if (stored) {
      ram.qr = new Uint8Array(stored.bytes); // 304, or BBMS unreachable: use the copy in flash
    }
  }

  async function checkIn(event) {
    const result = await api("POST", "check-in", {
      event,
      firmware_version: FIRMWARE_VERSION,
      cache_version: flash.get("cacheVersion", null),
    });
    if (!result) return;
    if (!result.ok) {
      ram.nextCheckIn = Date.now() + RETRY_SECONDS * 1000;
      draw();
      return;
    }
    const data = result.data;
    ram.heartbeatSeconds = data.heartbeat_seconds || ram.heartbeatSeconds;
    ram.sessionHeartbeatSeconds = data.session_heartbeat_seconds || ram.sessionHeartbeatSeconds;
    ram.nextCheckIn = Date.now() + ram.heartbeatSeconds * 1000;
    if (data.cache_version !== undefined) flash.set("cacheVersion", data.cache_version);
    const commands = data.commands || [];
    ram.disabled = commands.includes("disable");
    for (const command of commands) {
      if (command === "refresh_cache" || command === "purge_cache") {
        logNote(`command "${command}": the offline fob cache isn't simulated`);
      }
    }
    if (data.machine) applyMachine(data.machine);
    draw();
    if (commands.includes("reboot")) reboot();
  }

  function logNote(text) {
    const li = document.createElement("li");
    li.textContent = new Date().toLocaleTimeString("en-GB") + "  " + text;
    $("log").prepend(li);
  }

  // The machine's details, from check-in and most replies.
  function applyMachine(machine) {
    ram.machine = machine;
    if (ram.session && machine.session_id !== ram.session.id && !ram.pendingEnd) {
      // Ended elsewhere (an admin, the tidy-up or idle auto-end).
      ram.session = null;
      showMessage(["Session ended", "by BBMS"]);
    }
    if (!ram.session && machine.session_id && !ram.pendingEnd) {
      ram.orphan = { id: machine.session_id, name: machine.in_use_by };
    } else if (!machine.session_id) {
      ram.orphan = null;
    }
    const threshold = machine.meter && machine.meter.current_threshold_amps;
    $("threshold").textContent =
      machine.meter && machine.meter.mode === "current"
        ? `(threshold ${threshold} A)`
        : "(this machine is metered by tapped-in time)";
    updateRelay();
  }

  // ---- Fob taps and buttons ------------------------------------------------------------------------------------
  async function tap() {
    const card = $("card").value.trim();
    if (!powered || ram.booting || !card) return;
    if (ram.disabled) return showMessage(["Reader disabled", "by an admin"]);
    if (ram.session) return endSession({ card }, ram.session.id);

    const result = await api("POST", "access", { card });
    if (!result) return;
    if (!result.ok) return showMessage(["BBMS unavailable", "Try again shortly"]);
    const data = result.data;
    if (data.machine) applyMachine(data.machine);
    if (data.allowed) {
      startLocalSession(data);
    } else {
      showMessage(refusalText(data));
    }
  }

  function startLocalSession(data) {
    const now = Date.now();
    ram.orphan = null;
    ram.session = {
      id: data.session_id,
      name: data.display_name || "",
      reason: data.session_reason || "",
      rate: data.hourly_rate_pence ?? (ram.machine ? ram.machine.hourly_rate_pence : 0),
      // Updated by each heartbeat; the screen counts on from these between heartbeats.
      atHeartbeat: now,
      elapsed: 0,
      fee: 0,
      balance: data.balance_pence ?? 0,
    };
    ram.nextSessionHeartbeat = now; // straight away, to learn the elapsed time and fee (e.g. when resuming)
    updateRelay();
    showMessage([data.resumed ? "Welcome back" : "Hello", ram.session.name], 2);
  }

  async function sessionHeartbeat() {
    const session = ram.session;
    ram.nextSessionHeartbeat = Date.now() + ram.sessionHeartbeatSeconds * 1000;
    const result = await api("POST", `sessions/${session.id}/heartbeat`, {});
    if (!result || ram.session !== session) return;
    if (result.status === 404) {
      endLocalSession();
      return showMessage(["Session not found", "Tap in again"]);
    }
    if (!result.ok) return; // keep counting locally; try again next time
    const data = result.data;
    if (data.open === false) {
      endLocalSession();
      return showMessage(summaryText(data), 8);
    }
    Object.assign(session, {
      atHeartbeat: Date.now(),
      elapsed: data.elapsed_seconds ?? session.elapsed,
      fee: data.fee_so_far_pence ?? session.fee,
      balance: data.balance_pence ?? session.balance,
    });
    draw();
  }

  async function stopButton() {
    if (!powered || ram.booting) return;
    if (ram.session) return endSession({ ended_by: "button" }, ram.session.id);
    if (ram.orphan) return endSession({ ended_by: "button" }, ram.orphan.id); // left open by a power cut
    showMessage(["Nothing to stop"], 2);
  }

  async function endSession(body, id) {
    await flushMeter(true);
    const result = await api("POST", `sessions/${id}/end`, body);
    if (!result) return; // the power was cut while waiting
    if (!result.ok) {
      // Turn the machine off anyway; tell BBMS when it's back.
      ram.pendingEnd = { id, body };
      ram.nextEndRetry = Date.now() + RETRY_SECONDS * 1000;
      endLocalSession();
      return showMessage(["Session ended", "BBMS will be told", "when it's back"]);
    }
    const data = result.data;
    if (data.ended === false) {
      if (data.machine) applyMachine(data.machine);
      return showMessage(refusalText(data)); // someone else's fob: the session carries on
    }
    endLocalSession();
    if (data.machine) applyMachine(data.machine);
    showMessage(summaryText(data), 8);
  }

  async function retryPendingEnd() {
    const pending = ram.pendingEnd;
    ram.nextEndRetry = Date.now() + RETRY_SECONDS * 1000;
    const result = await api("POST", `sessions/${pending.id}/end`, pending.body);
    if (!result || !result.ok) return;
    ram.pendingEnd = null;
    ram.nextEndRetry = Infinity;
    if (result.data.machine) applyMachine(result.data.machine);
    draw();
  }

  function endLocalSession() {
    ram.session = null;
    ram.orphan = null;
    ram.nextSessionHeartbeat = Infinity;
    updateRelay();
    draw();
  }

  async function faultButton() {
    if (!powered || ram.booting) return;
    if (!ram.session) return showMessage(["Tap in first", "to report a fault"]);
    const broken = ram.machine && ram.machine.status === "out_of_order";
    const status = broken ? "working" : "out_of_order";
    const result = await api("POST", "machine-status", { status, session_id: ram.session.id, note: "" });
    if (!result) return;
    if (!result.ok) return showMessage(["BBMS unavailable", "Fault not reported"]);
    if (result.data.machine) applyMachine(result.data.machine);
    if (result.data.changed) {
      showMessage(status === "working" ? ["Marked as fixed", "Thank you"] : ["Reported out of order", "The team will be told"]);
    } else {
      showMessage([result.data.reason || "No change"]);
    }
  }

  // ---- Hour meter ----------------------------------------------------------------------------------------------
  function relayOn() {
    return powered && ram && ram.session !== null;
  }

  function measuredAmps() {
    return relayOn() ? parseFloat($("current").value) : 0;
  }

  function meteringByCurrent() {
    return ram.machine && ram.machine.meter && ram.machine.meter.mode === "current";
  }

  // Queue a reading (kept in flash) and try to send everything queued.
  async function flushMeter(force) {
    ram.nextMeterQueue = Date.now() + METER_REPORT_SECONDS * 1000;
    if (!meteringByCurrent()) return;
    if (force || ram.activeSeconds > 0 || ram.session) {
      const seq = flash.get("meterSeq", 0) + 1;
      flash.set("meterSeq", seq);
      const queue = flash.get("meterQueue", []);
      queue.push({ seq, active_seconds: ram.activeSeconds, session_id: ram.session ? ram.session.id : null });
      flash.set("meterQueue", queue);
      ram.activeSeconds = 0;
    }
    await sendMeterQueue();
  }

  async function sendMeterQueue() {
    let queue = flash.get("meterQueue", []);
    while (queue.length) {
      const result = await api("POST", "meter", queue[0]);
      if (!result) return;
      if (!result.ok) {
        ram.nextMeterSend = Date.now() + RETRY_SECONDS * 1000;
        return; // keep it; the same seq is sent again later, and BBMS ignores repeats
      }
      queue = flash.get("meterQueue", []).filter((reading) => reading.seq !== queue[0].seq);
      flash.set("meterQueue", queue);
    }
  }

  // ---- The once-a-second loop -----------------------------------------------------------------------------------
  let busy = false;

  async function tick() {
    if (!powered || !ram || ram.booting) return;
    const now = Date.now();
    const threshold = ram.machine && ram.machine.meter ? parseFloat(ram.machine.meter.current_threshold_amps) : NaN;
    if (meteringByCurrent() && measuredAmps() > threshold) ram.activeSeconds += 1;
    if (ram.message && now > ram.message.until) ram.message = null;
    draw();

    if (busy) return; // one request at a time, like a small microcontroller
    busy = true;
    try {
      if (now >= ram.nextCheckIn) await checkIn("heartbeat");
      else if (ram.session && now >= ram.nextSessionHeartbeat) await sessionHeartbeat();
      else if (ram.pendingEnd && now >= ram.nextEndRetry) await retryPendingEnd();
      else if (now >= ram.nextMeterQueue) await flushMeter(false);
      else if (flash.get("meterQueue", []).length && now >= ram.nextMeterSend) await sendMeterQueue();
    } finally {
      busy = false;
    }
  }

  // ---- Screen -------------------------------------------------------------------------------------------------
  const W = screen.width;
  const H = screen.height;

  function showMessage(lines, seconds = MESSAGE_SECONDS) {
    if (!ram) return;
    ram.message = { lines, until: Date.now() + seconds * 1000 };
    draw();
  }

  function pounds(pence) {
    const sign = pence < 0 ? "-" : "";
    return `${sign}£${(Math.abs(pence) / 100).toFixed(2)}`;
  }

  function duration(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  const REFUSALS = {
    unknown_fob: ["Fob not recognised"],
    fob_lost: ["This fob was", "reported lost"],
    not_member: ["Not a current member"],
    not_trained: ["Training needed", "for this machine"],
    course_incomplete: ["Finish the online", "course first"],
    out_of_order: ["Out of order"],
    insufficient_credit: ["Not enough credit", "Please top up"],
    device_disabled: ["Reader disabled"],
    not_tapped_in: ["Tap in first"],
  };

  function refusalText(data) {
    if (data.reason === "in_use") return ["In use by", data.in_use_by || "another member"];
    return REFUSALS[data.reason] || ["Refused", data.reason || "?"];
  }

  function summaryText(data) {
    if (data.removed) return ["Session ended", "Under a minute:", "no charge"];
    return [
      "Session ended " + duration(data.duration_seconds || 0),
      "Charge " + pounds(data.fee_pence || 0),
      "Balance " + pounds(data.balance_pence || 0),
    ];
  }

  // What the session screen shows between heartbeats: counted on locally from the hourly rate.
  function liveSession() {
    const s = ram.session;
    const elapsed = s.elapsed + (Date.now() - s.atHeartbeat) / 1000;
    const minimum = ((ram.machine && ram.machine.minimum_minutes) || 15) * 60;
    const fee = s.reason ? 0 : Math.round((s.rate * Math.max(elapsed, minimum)) / 3600);
    return { elapsed, fee, balance: s.balance + s.fee - fee };
  }

  function text(str, x, y, size, { bold = false, align = "left" } = {}) {
    ctx.font = `${bold ? "bold " : ""}${size}px "DejaVu Sans", Verdana, sans-serif`;
    ctx.textAlign = align;
    ctx.fillText(str, x, y);
  }

  function wrap(str, maxWidth, size) {
    ctx.font = `${size}px "DejaVu Sans", Verdana, sans-serif`;
    const words = String(str).split(/\s+/);
    const lines = [];
    let line = "";
    for (const word of words) {
      const candidate = line ? line + " " + word : word;
      if (ctx.measureText(candidate).width > maxWidth && line) {
        lines.push(line);
        line = word;
      } else {
        line = candidate;
      }
    }
    if (line) lines.push(line);
    return lines;
  }

  function drawQr(bytes, x, y, scale) {
    const n = bytes[0];
    const rowBytes = Math.ceil(n / 8);
    for (let row = 0; row < n; row++) {
      for (let col = 0; col < n; col++) {
        const byte = bytes[1 + row * rowBytes + (col >> 3)];
        if ((byte >> (7 - (col & 7))) & 1) ctx.fillRect(x + col * scale, y + row * scale, scale, scale);
      }
    }
    return n * scale;
  }

  function draw() {
    if (!powered || !ram) return; // no power: the e-paper keeps its last picture
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = "#000";
    ctx.textBaseline = "alphabetic";

    if (ram.booting) {
      text("Starting...", W / 2, H / 2 + 6, 18, { bold: true, align: "center" });
    } else if (ram.message) {
      const lines = ram.message.lines;
      const top = H / 2 - (lines.length - 1) * 13 + 7;
      lines.forEach((line, i) => text(line, W / 2, top + i * 26, i === 0 ? 20 : 17, { bold: i === 0, align: "center" }));
    } else if (ram.session) {
      drawSession();
    } else {
      drawIdle();
    }
    toOneBit();
  }

  function drawSession() {
    const s = ram.session;
    const live = liveSession();
    text(ram.machine ? ram.machine.name : "", 6, 16, 13);
    if (s.reason) {
      ctx.fillRect(W - 102, 3, 98, 17);
      ctx.fillStyle = "#fff";
      text(s.reason.toUpperCase(), W - 53, 16, 11, { bold: true, align: "center" });
      ctx.fillStyle = "#000";
    }
    ctx.fillRect(0, 22, W, 1);
    text(s.name, 6, 50, 22, { bold: true });
    text(duration(live.elapsed), 6, 80, 22);
    text(s.reason ? "No charge" : "Charge " + pounds(live.fee), 6, 102, 15);
    text("Balance " + pounds(live.balance), 6, 122, 15, { bold: live.balance < 0 });
    text("Tap or Stop to end", W - 6, 122, 11, { align: "right" });
    if (!ram.online) text("offline", W - 6, 102, 11, { align: "right" });
  }

  function drawIdle() {
    const m = ram.machine;
    let qrWidth = 0;
    if (ram.qr) qrWidth = drawQr(ram.qr, W - ram.qr[0] * 3 - 8, Math.floor((H - ram.qr[0] * 3) / 2), 3) + 16;
    const textWidth = W - qrWidth - 12;

    if (ram.disabled) {
      text("Reader disabled", 6, 40, 18, { bold: true });
      text("Ask an admin", 6, 64, 14);
      return;
    }
    if (!m) {
      text(ram.online ? "Not set up" : "BBMS unavailable", 6, 40, 17, { bold: true });
      text(ram.online ? "No machine for this reader" : "Retrying...", 6, 64, 13);
      return;
    }
    wrap(m.name, textWidth, 18).slice(0, 2).forEach((line, i) => text(line, 6, 24 + i * 20, 18, { bold: true }));
    if (m.status === "out_of_order") {
      ctx.fillRect(4, 52, textWidth, 22);
      ctx.fillStyle = "#fff";
      text("OUT OF ORDER", 4 + textWidth / 2, 68, 15, { bold: true, align: "center" });
      ctx.fillStyle = "#000";
      wrap(m.status_note || "", textWidth, 12).slice(0, 2).forEach((line, i) => text(line, 6, 90 + i * 14, 12));
    } else if (ram.orphan) {
      text("Session open:", 6, 66, 13);
      text(ram.orphan.name || "", 6, 84, 15, { bold: true });
      text("Tap to resume, or Stop", 6, 102, 12);
    } else {
      const rate = m.hourly_rate_pence ? pounds(m.hourly_rate_pence) + " / hour" : "Free to use";
      text(rate, 6, 70, 17);
      if (m.hourly_rate_pence) text(`Minimum ${m.minimum_minutes} minutes`, 6, 88, 11);
      text("Tap your fob to start", 6, 116, 13);
    }
    if (!ram.online) text("offline", 6, H - 2, 10);
  }

  // Make it look like e-paper: every pixel black or white, no anti-aliasing.
  function toOneBit() {
    const image = ctx.getImageData(0, 0, W, H);
    const px = image.data;
    for (let i = 0; i < px.length; i += 4) {
      const dark = px[i] + px[i + 1] + px[i + 2] < 384;
      px[i] = px[i + 1] = px[i + 2] = dark ? 20 : 233;
      px[i + 3] = 255;
    }
    ctx.putImageData(image, 0, 0);
  }

  function updateRelay() {
    $("led").classList.toggle("on", relayOn());
  }

  // ---- Wiring the page up -------------------------------------------------------------------------------------
  $("key").value = flash.get("apiKey", "");
  $("key").addEventListener("change", () => {
    flash.set("apiKey", $("key").value.trim());
    flash.set("qr", null); // a different device has a different QR code
    flash.set("cacheVersion", null);
    flash.set("meterQueue", []);
    if (powered) reboot();
  });
  $("tap").addEventListener("click", tap);
  $("card").addEventListener("keydown", (event) => {
    if (event.key === "Enter") tap();
  });
  $("stop").addEventListener("click", stopButton);
  $("fault").addEventListener("click", faultButton);
  $("power").addEventListener("change", () => ($("power").checked ? powerOn() : powerOff()));
  $("current").addEventListener("input", () => {
    $("amps").textContent = parseFloat($("current").value).toFixed(1);
  });
  $("checkin").addEventListener("click", () => {
    if (powered && ram && !ram.booting) ram.nextCheckIn = 0;
  });
  $("clearlog").addEventListener("click", () => ($("log").innerHTML = ""));

  powerOn();
})();
