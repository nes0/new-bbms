// Membership trend charts on the stats page (spec 06 §6.13). The figures are also in a table on the page.
(function () {
  if (typeof Chart === "undefined") return; // the table still shows the figures
  const rows = JSON.parse(document.getElementById("trend-data").textContent);
  const css = getComputedStyle(document.querySelector(".viz-root"));
  const v = (name) => css.getPropertyValue(name).trim();
  const labels = rows.map((r) => r.label);
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.color = v("--viz-text-secondary");
  const axes = {
    x: {
      grid: { display: false },
      border: { color: v("--viz-grid") },
      ticks: { maxRotation: 0, autoSkip: true, autoSkipPadding: 16 },
    },
    y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: v("--viz-grid") }, border: { display: false } },
  };
  const tooltip = { backgroundColor: v("--viz-surface"), titleColor: v("--viz-text"), bodyColor: v("--viz-text"),
    borderColor: v("--viz-grid"), borderWidth: 1 };

  const estimated = (ctx) => rows[ctx.p1DataIndex].estimated;
  new Chart(document.getElementById("members-chart"), {
    type: "line",
    data: {
      labels,
      datasets: [{
        label: "Members",
        data: rows.map((r) => r.members),
        borderColor: v("--viz-series-1"),
        backgroundColor: v("--viz-series-1"),
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 5,
        segment: { borderDash: (ctx) => (estimated(ctx) ? [5, 4] : undefined) },
      }],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: axes,
      plugins: {
        legend: { display: false }, // one series: the heading names it
        tooltip: { ...tooltip, callbacks: { afterLabel: (item) => (rows[item.dataIndex].estimated ? "Estimated" : "") } },
      },
    },
  });

  new Chart(document.getElementById("flow-chart"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "Joined", data: rows.map((r) => r.joined), backgroundColor: v("--viz-series-1") },
        { label: "Left", data: rows.map((r) => r.left), backgroundColor: v("--viz-series-2") },
      ].map((d) => ({ ...d, borderRadius: 4, borderSkipped: "start", maxBarThickness: 14, borderColor: v("--viz-surface"), borderWidth: 1 })),
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: axes,
      plugins: {
        legend: { position: "top", align: "start", labels: { boxWidth: 12, boxHeight: 12, useBorderRadius: true, borderRadius: 2 } },
        tooltip: { ...tooltip, callbacks: { afterTitle: (items) => (rows[items[0].dataIndex].estimated ? "Estimated" : "") } },
      },
    },
  });
})();
