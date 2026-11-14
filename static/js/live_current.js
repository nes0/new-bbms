// A machine's live current in the admin (templates/admin/equipment/equipment/live_current.html, spec 05 §5.4).
(function () {
  "use strict";
  const url = JSON.parse(document.getElementById("readings-url").textContent);
  const svg = document.getElementById("chart");
  const NS = "http://www.w3.org/2000/svg";
  const W = 720, H = 260, LEFT = 44, RIGHT = 12, TOP = 12, BOTTOM = 26;
  let data = null;

  function el(name, attrs, text) {
    const node = document.createElementNS(NS, name);
    for (const key in attrs) node.setAttribute(key, attrs[key]);
    if (text !== undefined) node.textContent = text;
    svg.appendChild(node);
    return node;
  }
  function amps(value) { return value.toFixed(2) + " A"; }
  function niceTop(value) {
    // The top of the scale: a round number a little above the highest reading, divisible into 4 grid lines.
    for (let scale = 0.1; scale <= 1000; scale *= 10)
      for (const step of [1, 2, 3, 4, 5, 6, 8]) if (value <= step * scale) return step * scale;
    return 10000;
  }

  function draw() {
    while (svg.lastChild && svg.lastChild.nodeName !== "title") svg.removeChild(svg.lastChild);
    if (!data) return;
    const start = data.now - data.window_ms;
    const highest = Math.max(data.threshold || 0, ...data.samples.map((s) => s[1]), 0.5);
    const top = niceTop(highest * 1.1);
    const x = (t) => LEFT + ((t - start) / data.window_ms) * (W - LEFT - RIGHT);
    const y = (a) => TOP + (1 - a / top) * (H - TOP - BOTTOM);

    for (let i = 0; i <= 4; i++) {
      const value = (top * i) / 4;
      el("line", { class: "grid", x1: LEFT, x2: W - RIGHT, y1: y(value), y2: y(value) });
      el("text", { class: "axis-label", x: LEFT - 6, y: y(value) + 4, "text-anchor": "end" }, String(+value.toFixed(3)));
    }
    for (let minutes = 5; minutes >= 0; minutes--) {
      const t = data.now - minutes * 60000;
      el("text", { class: "axis-label", x: x(t), y: H - 6, "text-anchor": "middle" },
         minutes ? `-${minutes} min` : "now");
    }
    if (data.threshold !== null) {
      el("line", { class: "threshold", x1: LEFT, x2: W - RIGHT, y1: y(data.threshold), y2: y(data.threshold) });
      el("text", { class: "threshold-label", x: W - RIGHT, y: y(data.threshold) - 5, "text-anchor": "end" },
         "threshold " + amps(data.threshold));
    }
    // A gap of more than a few seconds between samples is drawn as a break, not a slope.
    let path = "", previous = null;
    for (const [t, a] of data.samples) {
      path += (previous === null || t - previous > 5000 ? "M" : "L") + x(t).toFixed(1) + " " + y(a).toFixed(1);
      previous = t;
    }
    if (path) el("path", { class: "line", d: path });
    svg._scale = { x, y, start };
  }

  function update() {
    const s = data.samples;
    document.getElementById("latest").textContent = s.length ? amps(s[s.length - 1][1]) : "–";
    document.getElementById("low").textContent = data.last_minute ? amps(data.last_minute.min) : "–";
    document.getElementById("high").textContent = data.last_minute ? amps(data.last_minute.max) : "–";
    const status = document.getElementById("status");
    if (!data.reporting) status.textContent = "Not reporting.";
    else if (!data.last_sample_at || data.now - data.last_sample_at > 15000)
      status.textContent = "Reporting is on: waiting for the reader…";
    else status.textContent = "Reporting until " + new Date(data.until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + ".";
    draw();
  }

  async function poll() {
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (response.ok) { data = await response.json(); update(); }
    } catch (error) { /* try again next time */ }
    setTimeout(poll, data && data.reporting ? 2000 : 10000);
  }

  // Hover: the reading nearest the pointer.
  let cursor = null, dot = null;
  svg.addEventListener("pointermove", (event) => {
    if (!data || !data.samples.length || !svg._scale) return;
    const box = svg.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * W;
    const { x, y } = svg._scale;
    let best = data.samples[0];
    for (const sample of data.samples) if (Math.abs(x(sample[0]) - px) < Math.abs(x(best[0]) - px)) best = sample;
    if (!cursor) { cursor = document.createElementNS(NS, "line"); cursor.setAttribute("class", "cursor"); }
    if (!dot) { dot = document.createElementNS(NS, "circle"); dot.setAttribute("class", "dot"); dot.setAttribute("r", 4); }
    cursor.setAttribute("x1", x(best[0])); cursor.setAttribute("x2", x(best[0]));
    cursor.setAttribute("y1", TOP); cursor.setAttribute("y2", H - BOTTOM);
    dot.setAttribute("cx", x(best[0])); dot.setAttribute("cy", y(best[1]));
    svg.appendChild(cursor); svg.appendChild(dot);
    document.getElementById("readout").textContent =
      new Date(best[0]).toLocaleTimeString() + ": " + amps(best[1]);
  });
  svg.addEventListener("pointerleave", () => {
    if (cursor) cursor.remove();
    if (dot) dot.remove();
    document.getElementById("readout").textContent = "";
  });

  poll();
})();
