/*
 * Fake exit node: a screen by the door listing who's in the space, as a small embedded Linux device with a
 * portrait panel (about the size of a tablet, no touchscreen) would, talking to BBMS through the v2 device API
 * (spec 05 §5.4 "Occupancy screen", docs/device-api-v2.md). Plain JavaScript, no build step.
 *
 * What's "hardware" here:
 *   - the panel (the dark 3:4 box); everything is laid out for a fixed panel and scaled;
 *   - a fob reader (the bench's Fob box and "Tap on the exit node"): the node's only input;
 *   - a network LED, green while BBMS answers.
 * "Disk" (kept through power cuts) is localStorage for the keys and the QR code, and a photo cache held for the
 * life of the page. Photos are dropped as soon as their member leaves the list, so the node never keeps photos of
 * people who aren't in. Everything else is lost when the power goes.
 *
 * The "Tap at the front door" button is the bench pretending to be a member arriving: it calls `access` with the
 * front door's key, so the list grows.
 */
"use strict";

(() => {
  const API = "/api/devices/v2/";
  const FIRMWARE_VERSION = "fake-exit-0.1";
  const OCCUPANCY_SECONDS = 30; // how often the list is fetched (cheap: 304 while nothing changes)
  const RETRY_SECONDS = 15;
  const TOAST_SECONDS = 4;
  const PAGE_SECONDS = 8; // more people than fit: show a page at a time
  const PHOTO_SIZE = 160; // pixels; PNG, which a Linux device decodes easily

  const $ = (id) => document.getElementById(id);

  // ---- Disk ------------------------------------------------------------------------------------------------------
  const disk = {
    get(name, fallback) {
      try {
        const value = localStorage.getItem("fakeExit." + name);
        return value === null ? fallback : JSON.parse(value);
      } catch {
        return fallback;
      }
    },
    set(name, value) {
      try {
        localStorage.setItem("fakeExit." + name, JSON.stringify(value));
      } catch {
        /* private window: the disk just doesn't persist */
      }
    },
  };
  const photoCache = new Map(); // "id:version" -> object URL

  // ---- RAM -------------------------------------------------------------------------------------------------------
  let ram = null;
  let powered = false;
  let tickTimer = null;

  function freshRam() {
    return {
      generation: (ram ? ram.generation : 0) + 1, // replies from before a power cut are ignored
      booting: true,
      online: false,
      disabled: false,
      heartbeatSeconds: 300,
      nextCheckIn: Infinity,
      nextOccupancy: 0,
      etag: null,
      list: null, // the last occupancy reply
      fetchedAt: null, // when it was last confirmed by BBMS
      page: 0,
      nextPage: Date.now() + PAGE_SECONDS * 1000,
      toast: null, // {main, small, kind, until}
    };
  }

  // ---- Talking to BBMS -------------------------------------------------------------------------------------------
  // Returns null if the power was cut while waiting; otherwise {ok, status, data, response}.
  async function api(method, path, body, { key = $("key").value.trim(), headers = {}, blob = false } = {}) {
    const generation = ram.generation;
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
        if (blob && response.status === 200) data = await response.blob();
        else if ((response.headers.get("Content-Type") || "").includes("json")) {
          data = await response.json().catch(() => null);
        }
        result = { ok: response.status === 200 && data !== null, status: response.status, data, response };
      } catch (error) {
        result = { ok: false, status: 0, data: null, response: null, error: String(error) };
      }
    }
    logResponse(entry, result);
    if (!powered || !ram || ram.generation !== generation) return null;
    if (key === $("key").value.trim()) {
      ram.online = result.ok || result.status === 304 || (result.status >= 400 && result.status < 500);
      $("led").classList.toggle("on", ram.online);
    }
    return result;
  }

  // ---- Request log -----------------------------------------------------------------------------------------------
  function logRequest(method, path, body) {
    const li = document.createElement("li");
    li.textContent = `${new Date().toLocaleTimeString("en-GB")}  ${method} ${path}`;
    if (body !== undefined) li.appendChild(pre("→ " + JSON.stringify(body)));
    $("log").prepend(li);
    while ($("log").children.length > 150) $("log").lastChild.remove();
    return li;
  }

  function logResponse(li, result) {
    let text = "← " + (result.status || "no response");
    if (result.error) text += "  " + result.error;
    if (result.data instanceof Blob) text += `  (${result.data.type}, ${result.data.size} bytes)`;
    else if (result.data) text += "  " + JSON.stringify(result.data, null, 1).replace(/\n\s*/g, " ");
    const line = pre(text);
    if (!result.ok && result.status !== 304) line.classList.add("bad");
    li.appendChild(line);
  }

  function logNote(text) {
    const li = document.createElement("li");
    li.textContent = new Date().toLocaleTimeString("en-GB") + "  " + text;
    $("log").prepend(li);
  }

  function pre(text) {
    const element = document.createElement("pre");
    element.textContent = text;
    return element;
  }

  // ---- Start-up and check-in -------------------------------------------------------------------------------------
  async function powerOn() {
    powered = true;
    $("power").checked = true;
    ram = freshRam();
    $("panel").classList.remove("off");
    $("boot").hidden = false;
    $("boot").textContent = "Starting…";
    clearInterval(tickTimer);
    tickTimer = setInterval(tick, 1000);
    if (!$("key").value.trim()) {
      $("boot").textContent = "No API key: paste one on the bench";
      return;
    }
    await loadQr();
    if (!ram) return;
    await checkIn("boot");
    if (!ram) return;
    await fetchOccupancy();
    if (!ram) return;
    ram.booting = false;
    $("boot").hidden = true;
    render();
  }

  function powerOff() {
    powered = false;
    $("power").checked = false;
    clearInterval(tickTimer);
    tickTimer = null;
    if (ram) ram.generation += 1;
    $("panel").classList.add("off"); // a backlit panel goes dark
    $("led").classList.remove("on");
  }

  async function reboot() {
    powerOff();
    await new Promise((resolve) => setTimeout(resolve, 800));
    powerOn();
  }

  async function checkIn(event) {
    // This node keeps no offline fob cache, but sends back the version BBMS gave it, so it isn't asked to refresh.
    const cache_version = disk.get("cacheVersion", "");
    const result = await api("POST", "check-in", { event, firmware_version: FIRMWARE_VERSION, cache_version });
    if (!result) return;
    if (!result.ok) {
      ram.nextCheckIn = Date.now() + RETRY_SECONDS * 1000;
      return;
    }
    const data = result.data;
    ram.heartbeatSeconds = data.heartbeat_seconds || ram.heartbeatSeconds;
    ram.nextCheckIn = Date.now() + ram.heartbeatSeconds * 1000;
    const commands = data.commands || [];
    if (data.cache_version !== undefined) disk.set("cacheVersion", data.cache_version);
    ram.disabled = commands.includes("disable");
    for (const command of commands) {
      if (command === "refresh_cache" || command === "purge_cache") {
        logNote(`command "${command}": this node keeps no fob cache`);
      }
    }
    if (commands.includes("reboot")) reboot();
  }

  // ---- The QR code (raw format, spec 05 §5.4) -------------------------------------------------------------------
  async function loadQr() {
    const stored = disk.get("qr", null); // {etag, bytes}
    const headers = stored ? { "If-None-Match": stored.etag } : {};
    const result = await api("GET", "qr?format=json", undefined, { headers });
    if (!result) return;
    if (result.status === 200 && result.data) {
      disk.set("qr", { etag: result.response.headers.get("ETag"), rows: result.data.rows, size: result.data.size });
      drawQr(result.data.size, result.data.rows);
    } else if (stored) {
      drawQr(stored.size, stored.rows);
    }
  }

  function drawQr(size, rows) {
    const canvas = $("qr");
    canvas.width = canvas.height = size;
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, size, size);
    ctx.fillStyle = "#000";
    rows.forEach((hex, y) => {
      for (let x = 0; x < size; x++) {
        const byte = parseInt(hex.substr((x >> 3) * 2, 2), 16);
        if (byte & (0x80 >> (x & 7))) ctx.fillRect(x, y, 1, 1);
      }
    });
  }

  // ---- Who's in --------------------------------------------------------------------------------------------------
  async function fetchOccupancy() {
    ram.nextOccupancy = Date.now() + OCCUPANCY_SECONDS * 1000;
    const headers = ram.etag ? { "If-None-Match": ram.etag } : {};
    const result = await api("GET", "occupancy", undefined, { headers });
    if (!result) return;
    if (result.status === 304) {
      ram.fetchedAt = Date.now();
    } else if (result.ok) {
      ram.list = result.data;
      ram.etag = result.response.headers.get("ETag");
      ram.fetchedAt = Date.now();
      await loadPhotos(result.data.people);
    } else {
      ram.nextOccupancy = Date.now() + RETRY_SECONDS * 1000;
    }
    render();
  }

  async function loadPhotos(people) {
    const wanted = new Set(people.filter((p) => p.id && p.photo).map((p) => `${p.id}:${p.photo}`));
    for (const [key, url] of photoCache) {
      if (!wanted.has(key)) {
        URL.revokeObjectURL(url); // not in any more, or a new photo: forget it
        photoCache.delete(key);
      }
    }
    for (const key of wanted) {
      if (photoCache.has(key)) continue;
      const id = key.split(":")[0];
      const result = await api("GET", `photos/${id}?format=png&size=${PHOTO_SIZE}`, undefined, { blob: true });
      if (!result) return;
      if (result.ok) photoCache.set(key, URL.createObjectURL(result.data));
    }
  }

  // ---- Fob taps --------------------------------------------------------------------------------------------------
  const REFUSALS = {
    unknown_fob: ["Fob not recognised", "Ask a trustee to check it"],
    fob_lost: ["This fob was reported lost", "Ask a trustee"],
    not_member: ["Not a current member", "Your fob isn't active"],
    device_disabled: ["This reader is switched off", "Tapping out isn't recorded"],
  };

  async function tap() {
    const card = $("card").value.trim();
    if (!powered || !ram || ram.booting || !card) return;
    flashReader();
    if (ram.disabled) return toast("This reader is switched off", "by an admin", "refused");
    const result = await api("POST", "access", { card });
    if (!result) return;
    if (!result.ok) return toast("Can't reach BBMS", "Your tap wasn't recorded. Try again shortly.", "unavailable");
    const data = result.data;
    const still = data.in_space === 1 ? "1 person still in" : `${data.in_space} people still in`;
    if (data.reason === "checked_out") {
      toast(`Bye, ${data.display_name}`, data.in_space ? still : "The space is empty now", "ok");
    } else if (data.reason === "enrolled") {
      toast("Fob registered", data.display_name || "", "ok");
    } else {
      const [main, small] = REFUSALS[data.reason] || ["Not recorded", data.reason];
      toast(main, small, "refused");
    }
    ram.etag = null; // the list has changed: fetch it in full
    await fetchOccupancy();
  }

  async function tapAtDoor() {
    const card = $("card").value.trim();
    const key = $("door-key").value.trim();
    if (!card) return;
    if (!key) return logNote("paste the front door's key on the bench to tap people in");
    if (!ram) return;
    const result = await api("POST", "access", { card }, { key });
    if (result && result.ok && powered) {
      ram.nextOccupancy = Date.now() + 1500; // the node notices at its next poll; sooner here, for the demo
    }
  }

  function flashReader() {
    const ring = document.querySelector(".reader-ring");
    ring.classList.add("read");
    setTimeout(() => ring.classList.remove("read"), 400);
  }

  function toast(main, small, kind) {
    ram.toast = { main, small, kind, until: Date.now() + TOAST_SECONDS * 1000 };
    render();
  }

  // ---- The once-a-second loop ------------------------------------------------------------------------------------
  let busy = false;

  async function tick() {
    if (!powered || !ram || ram.booting) return;
    const now = Date.now();
    if (ram.toast && now > ram.toast.until) ram.toast = null;
    if (now >= ram.nextPage) {
      ram.page += 1;
      ram.nextPage = now + PAGE_SECONDS * 1000;
    }
    render();
    if (busy) return;
    busy = true;
    try {
      if (now >= ram.nextCheckIn) await checkIn("heartbeat");
      else if (now >= ram.nextOccupancy) await fetchOccupancy();
    } finally {
      busy = false;
    }
  }

  // ---- The screen ------------------------------------------------------------------------------------------------
  const COLOURS = ["#3d6fd1", "#c4502f", "#2f8f5b", "#8a55b5", "#b38a1d", "#26878f", "#b0406f"];

  function hhmm(unix) {
    return new Date(unix * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }

  function initials(name) {
    return name
      .replace(/\./g, "")
      .split(/\s+/)
      .map((part) => part[0] || "")
      .join("")
      .slice(0, 2)
      .toUpperCase();
  }

  function personRow(person) {
    const li = document.createElement("li");
    li.className = "person";
    const photo = person.id && person.photo ? photoCache.get(`${person.id}:${person.photo}`) : null;
    let avatar;
    if (photo) {
      avatar = document.createElement("img");
      avatar.src = photo;
      avatar.alt = "";
    } else {
      avatar = document.createElement("span");
      if (person.name) {
        avatar.textContent = initials(person.name);
        avatar.style.background = COLOURS[person.id % COLOURS.length];
      }
    }
    avatar.className = "avatar";
    const who = document.createElement("div");
    who.className = "who";
    const name = document.createElement("p");
    name.className = person.name ? "name" : "name anonymous";
    name.textContent = person.name || "A member";
    const detail = document.createElement("p");
    detail.className = "detail";
    if (person.since) detail.append(`In since ${hhmm(person.since)}`);
    if (person.using.length) {
      if (person.since) detail.append(" · ");
      const using = document.createElement("span");
      using.className = "using";
      using.textContent = "Using " + person.using.join(", ");
      detail.append(using);
    }
    who.append(name, detail);
    li.append(avatar, who);
    return li;
  }

  function render() {
    if (!ram) return;
    $("clock").textContent = new Date().toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    const list = ram.list;
    $("count").textContent = list ? String(list.in_space) : "–";

    if (!ram.online) {
      $("stale").hidden = false;
      $("stale").textContent = ram.fetchedAt
        ? `Can't reach BBMS: this is the list from ${new Date(ram.fetchedAt).toLocaleTimeString("en-GB", {
            hour: "2-digit",
            minute: "2-digit",
          })}`
        : "Can't reach BBMS";
    } else {
      $("stale").hidden = true;
    }

    const people = list ? list.people : [];
    const ol = $("people");
    $("empty").hidden = people.length > 0;
    ol.hidden = people.length === 0;
    ol.replaceChildren();
    if (people.length) {
      // As many rows as fit; more than that are shown a page at a time.
      ol.append(personRow(people[0]));
      const rowHeight = ol.firstChild.getBoundingClientRect().height || 1;
      const style = getComputedStyle(ol);
      const space = ol.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
      const perPage = Math.max(1, Math.floor(space / rowHeight));
      const pages = Math.ceil(people.length / perPage);
      const page = ram.page % pages;
      ol.replaceChildren(...people.slice(page * perPage, (page + 1) * perPage).map(personRow));
      $("page").hidden = pages === 1;
      $("page").textContent = `Page ${page + 1} of ${pages}`;
    } else {
      $("page").hidden = true;
    }

    const t = ram.toast;
    $("toast").hidden = !t;
    if (t) {
      $("toast").className = "toast " + (t.kind === "ok" ? "" : t.kind);
      $("toast-main").textContent = t.main;
      $("toast-small").textContent = t.small;
    }
  }

  // ---- Wiring the page up ----------------------------------------------------------------------------------------
  // A link can carry the keys after a "#" (browsers never send that part to the server):
  // …/fake-exit/#key=…&door=…
  const fromLink = new URLSearchParams(location.hash.slice(1));
  if (fromLink.get("key") && fromLink.get("key") !== disk.get("apiKey", "")) {
    disk.set("apiKey", fromLink.get("key"));
    disk.set("qr", null);
    disk.set("cacheVersion", "");
  }
  if (fromLink.get("door")) disk.set("doorKey", fromLink.get("door"));
  if (location.hash) history.replaceState(null, "", location.pathname);
  $("key").value = disk.get("apiKey", "");
  $("door-key").value = disk.get("doorKey", "");
  $("key").addEventListener("change", () => {
    disk.set("apiKey", $("key").value.trim());
    disk.set("qr", null); // a different device has a different QR code
    disk.set("cacheVersion", "");
    for (const url of photoCache.values()) URL.revokeObjectURL(url);
    photoCache.clear();
    if (powered) reboot();
    else powerOn();
  });
  $("door-key").addEventListener("change", () => disk.set("doorKey", $("door-key").value.trim()));
  $("tap").addEventListener("click", tap);
  $("tap-door").addEventListener("click", tapAtDoor);
  $("card").addEventListener("keydown", (event) => {
    if (event.key === "Enter") tap();
  });
  $("power").addEventListener("change", () => ($("power").checked ? powerOn() : powerOff()));
  $("refresh").addEventListener("click", () => {
    if (powered && ram && !ram.booting) ram.nextOccupancy = 0;
  });
  $("clearlog").addEventListener("click", () => ($("log").innerHTML = ""));

  powerOn();
})();
