(function () {
  const payload = window.__PAYLOAD__;
  const root    = document.getElementById("charts");

  if (!payload.accounts.length) {
    root.innerHTML = `<div class="card">Nog geen data. Ga naar Config, sla credentials op en klik op "↻ Refresh nu".</div>`;
    return;
  }

  const totalCard     = document.getElementById("totaal-card");
  const totalVal      = document.getElementById("totaal-value");
  const baselineInp   = document.getElementById("baseline-date");
  const baselineReset = document.getElementById("baseline-reset");

  // ── Peildatum (groei sinds…) — keuze bewaard in localStorage ─────────────
  const BASELINE_KEY = "meesman_baseline_date";
  if (baselineInp) {
    baselineInp.value = localStorage.getItem(BASELINE_KEY) || "";
    baselineInp.addEventListener("change", () => {
      if (baselineInp.value) localStorage.setItem(BASELINE_KEY, baselineInp.value);
      else localStorage.removeItem(BASELINE_KEY);
      render();
    });
  }
  if (baselineReset) {
    baselineReset.addEventListener("click", () => {
      if (baselineInp) baselineInp.value = "";
      localStorage.removeItem(BASELINE_KEY);
      render();
    });
  }

  // Baseline-punt voor een rekening: laatste meting op of vóór de peildatum,
  // anders de eerste meting (= oude gedrag zonder peildatum).
  function baselineFor(acc, baselineDate) {
    const pts = acc.points || [];
    if (!pts.length) return null;
    if (!baselineDate) return { ts: acc.first_ts, value: acc.first_value };
    const cutoff = baselineDate + "T23:59:59";
    let candidate = null;
    for (const p of pts) {
      if (p.x <= cutoff) candidate = p;
      else break;
    }
    if (candidate) return { ts: candidate.x, value: candidate.y };
    return { ts: pts[0].x, value: pts[0].y };
  }

  let charts = [];

  function render() {
    charts.forEach((c) => c.destroy());
    charts = [];
    root.innerHTML = "";

    const baselineDate  = baselineInp ? baselineInp.value : "";
    const baselineLabel = baselineDate ? `sinds ${fmtDate(baselineDate)}` : "groei";

    // ── Totaalbanner ────────────────────────────────────────────────────────
    if (totalCard && totalVal) {
      let total = 0, baseTotal = 0;
      let totalDeposits = 0, trueRendement = 0;

      payload.accounts.forEach((a) => {
        const base = baselineFor(a, baselineDate);
        total     += a.current ?? 0;
        baseTotal += base ? base.value : (a.current ?? 0);
        totalDeposits += a.total_deposits ?? 0;
        trueRendement += a.true_rendement ?? 0;
      });

      const totalDelta = total - baseTotal;
      const totalPct   = baseTotal ? (totalDelta / baseTotal * 100) : null;
      const truePct    = totalDeposits ? (trueRendement / totalDeposits * 100) : null;

      let html = `<strong>Totaal vermogen:</strong> <span style="font-size:1.4em; font-weight:700; margin-left:8px">${fmtEur(total)}</span>`;

      if (totalDelta !== 0 && baseTotal) {
        const color = totalDelta >= 0 ? "#0a7a0a" : "#b00020";
        html += `<span style="margin-left:12px; color:${color}; font-size:0.9em">${fmtEurDelta(totalDelta)} (${fmtPct(totalPct)}) ${esc(baselineLabel)}</span>`;
      }

      if (totalDeposits > 0) {
        const color = trueRendement >= 0 ? "#0a7a0a" : "#b00020";
        html += `<br><small style="color:#666">Totale inleg: ${fmtEur(totalDeposits)} &nbsp;—&nbsp; `;
        html += `Echt rendement: <strong style="color:${color}">${fmtEurDelta(trueRendement)}`;
        if (truePct !== null) html += ` (${fmtPct(truePct)})`;
        html += `</strong></small>`;
      }

      totalVal.innerHTML = html;
      totalCard.style.display = "";
    }

    // ── Per-rekening kaarten ────────────────────────────────────────────────
    payload.accounts.forEach((acc) => {
      const card = document.createElement("div");
      card.className = "card";

      const current = acc.current ?? null;
      const changes = acc.changes ?? [];
      const base    = baselineFor(acc, baselineDate);

      const growthDelta = (current !== null && base && base.value) ? current - base.value : null;
      const growthPct   = (growthDelta !== null && base.value) ? (growthDelta / base.value * 100) : null;

      const totalDeposits = acc.total_deposits   ?? null;
      const trueRendement = acc.true_rendement   ?? null;
      const trueRendPct   = acc.true_rendement_pct ?? null;

      // ── Header ──
      const header = document.createElement("div");
      header.style.cssText = "display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px; margin-bottom:10px";

      let groeiBadge = "";
      if (growthDelta !== null && growthPct !== null) {
        const color = growthDelta >= 0 ? "#0a7a0a" : "#b00020";
        const arrow = growthDelta >= 0 ? "📈" : "📉";
        groeiBadge = `<span style="font-size:0.8em; color:${color}; font-weight:600; margin-left:8px">${arrow} ${fmtEurDelta(growthDelta)} (${fmtPct(growthPct)}) ${esc(baselineLabel)}</span>`;
      }

      let rendBadge = "";
      if (trueRendement !== null && totalDeposits !== null) {
        const color = trueRendement >= 0 ? "#0a7a0a" : "#b00020";
        rendBadge = `<div style="font-size:0.8em; color:#555; margin-top:2px">
          Inleg: ${fmtEur(totalDeposits)} &nbsp;|&nbsp;
          Echt rendement: <strong style="color:${color}">${fmtEurDelta(trueRendement)}${trueRendPct !== null ? ` (${fmtPct(trueRendPct)})` : ""}</strong>
        </div>`;
      }

      header.innerHTML = `
        <div>
          <div>
            <strong style="font-size:1.05em">${esc(acc.label)}</strong>
            <small style="color:#888; margin-left:6px">${esc(acc.account_number)}</small>
            ${groeiBadge}
          </div>
          ${rendBadge}
        </div>
        ${current != null ? `<div style="font-size:1.3em; font-weight:700">${fmtEur(current)}</div>` : ""}
      `;
      card.appendChild(header);

      // ── Chart ──
      const canvas = document.createElement("canvas");
      canvas.height = 110;
      card.appendChild(canvas);

      // ── Wijzigingen tabel ──
      if (changes.length) {
        const tbl = document.createElement("div");
        tbl.style.cssText = "margin-top:12px; font-size:12px";

        const rows = [...changes].reverse().map((c) => {
          const delta    = c.delta     ?? null;
          const deltaPct = c.delta_pct ?? null;
          const color    = delta === null ? "" : delta >= 0 ? "color:#0a7a0a" : "color:#b00020";
          return `<tr>
            <td style="color:#555; padding-right:12px">${esc(fmtTs(c.ts))}</td>
            <td style="font-weight:600; text-align:right">${fmtEur(c.value)}</td>
            <td style="text-align:right; padding-left:10px; ${color}">
              ${delta !== null ? fmtEurDelta(delta) : '<span style="color:#ccc">—</span>'}
            </td>
            <td style="text-align:right; padding-left:6px; ${color}">
              ${deltaPct !== null ? `(${fmtPct(deltaPct)})` : ""}
            </td>
            <td style="text-align:right; padding-left:8px; width:24px">
              <button type="button" class="del-point" title="Datapunt verwijderen"
                      data-acc="${esc(acc.account_number)}" data-ts="${esc(c.ts)}"
                      data-info="${esc(fmtTs(c.ts))} — ${esc(fmtEur(c.value))}"
                      style="background:none; border:none; cursor:pointer;
                             color:#b00020; padding:2px 4px; font-size:13px">✕</button>
            </td>
          </tr>`;
        }).join("");

        tbl.innerHTML = `
          <strong style="display:block; margin-bottom:6px; color:#444">Wijzigingen</strong>
          <table style="width:100%">
            <thead><tr>
              <th style="text-align:left; color:#888; font-weight:normal">Tijdstip</th>
              <th style="text-align:right; color:#888; font-weight:normal">Saldo</th>
              <th style="text-align:right; color:#888; font-weight:normal" colspan="2">Δ t.o.v. vorig</th>
              <th></th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
          ${acc.first_ts && acc.first_value !== null
            ? `<p style="margin:8px 0 0; color:#888; font-size:11px">Eerste meting: ${esc(fmtTs(acc.first_ts))} — ${fmtEur(acc.first_value)}</p>`
            : ""}
        `;
        card.appendChild(tbl);

        // ── Datapunt verwijderen (met bevestiging) ──
        tbl.querySelectorAll(".del-point").forEach((btn) => {
          btn.addEventListener("click", () => {
            const d = btn.dataset;
            if (!confirm(`Weet je zeker dat je dit datapunt van ${esc(acc.label)} wilt verwijderen?\n\n${d.info}`)) return;
            fetch("/datapoints/delete", {
              method: "POST",
              headers: { "Content-Type": "application/x-www-form-urlencoded" },
              body: new URLSearchParams({ account_number: d.acc, ts: d.ts }),
            })
              .then((r) => {
                if (r.ok) location.reload();
                else r.json().then((j) => alert("Verwijderen mislukt: " + (j.detail || r.status)))
                             .catch(() => alert("Verwijderen mislukt (HTTP " + r.status + ")"));
              })
              .catch(() => alert("Verwijderen mislukt: netwerkfout"));
          });
        });
      }

      root.appendChild(card);

      // ── Chart.js ──
      charts.push(new Chart(canvas.getContext("2d"), {
        type: "line",
        data: {
          datasets: [{
            label:           acc.label,
            data:            acc.points,
            parsing:         { xAxisKey: "x", yAxisKey: "y" },
            tension:         0.3,
            pointRadius:     3,
            borderColor:     "#0066cc",
            backgroundColor: "rgba(0,102,204,0.08)",
            fill:            true,
          }],
        },
        options: {
          responsive: true,
          maintainAspectRatio: true,
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: (ctx) => ` ${fmtEur(ctx.parsed.y)}` } },
          },
          scales: {
            x: {
              type: "time",
              time: { tooltipFormat: "dd-MM-yyyy HH:mm" },
              ticks: { maxTicksLimit: 6, color: "#888" },
              grid:  { color: "#f0f0f0" },
            },
            y: {
              ticks: { callback: (v) => fmtEur(v), maxTicksLimit: 5, color: "#888" },
              grid:  { color: "#f0f0f0" },
            },
          },
        },
      }));
    });
  }

  render();

  // ── Helpers ───────────────────────────────────────────────────────────────
  function fmtEur(v) {
    if (v == null) return "—";
    return "€ " + Number(v).toLocaleString("nl-NL", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtEurDelta(v) {
    if (v == null) return "—";
    return (v >= 0 ? "+" : "-") + fmtEur(Math.abs(v));
  }
  function fmtPct(v) {
    if (v == null) return "";
    const sign = v >= 0 ? "+" : "";
    return sign + Number(v).toLocaleString("nl-NL", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + "%";
  }
  function fmtDate(isoDate) {
    const [y, m, d] = isoDate.split("-");
    return `${d}-${m}-${y}`;
  }
  function fmtTs(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString("nl-NL", {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit",
      });
    } catch { return iso; }
  }
  function esc(str) {
    return String(str).replace(/[&<>"']/g, (m) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]));
  }
})();
