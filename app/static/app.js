(function () {
  const payload = window.__PAYLOAD__;
  const root    = document.getElementById("charts");

  // Kleuren uit de CSS-variabelen (licht/donker thema)
  const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const C = {
    ok: cssVar("--ok"), err: cssVar("--err"), muted: cssVar("--muted"), muted2: cssVar("--muted2"),
    faint: cssVar("--faint"), accent: cssVar("--accent"), accentDark: cssVar("--accent-dark"),
    fill: cssVar("--accent-fill"), grid: cssVar("--grid"), text: cssVar("--text"),
  };

  if (!payload.accounts.length) {
    root.innerHTML = `<div class="card">Nog geen data. Ga naar Config, sla credentials op en klik op "↻ Refresh nu".</div>`;
    return;
  }

  const totalCard     = document.getElementById("totaal-card");
  const totalVal      = document.getElementById("totaal-value");
  const baselineInp   = document.getElementById("baseline-date");
  const baselineReset = document.getElementById("baseline-reset");
  const periodBtns    = document.getElementById("period-buttons");

  // ── Grafiekperiode (1m / 3m / 1j / alles) — keuze bewaard in localStorage ──
  const PERIOD_KEY = "meesman_chart_days";
  let periodDays = parseInt(localStorage.getItem(PERIOD_KEY) || "0", 10) || 0;

  function syncPeriodButtons() {
    if (!periodBtns) return;
    periodBtns.querySelectorAll("button").forEach((b) => {
      const active = parseInt(b.dataset.days, 10) === periodDays;
      b.style.background  = active ? C.accent : "";
      b.style.color       = active ? "#fff" : "";
      b.style.borderColor = active ? C.accentDark : "";
    });
  }
  if (periodBtns) {
    periodBtns.querySelectorAll("button").forEach((b) => {
      b.addEventListener("click", () => {
        periodDays = parseInt(b.dataset.days, 10) || 0;
        if (periodDays) localStorage.setItem(PERIOD_KEY, String(periodDays));
        else localStorage.removeItem(PERIOD_KEY);
        syncPeriodButtons();
        render();
      });
    });
    syncPeriodButtons();
  }

  // Punten voor de grafiek binnen de gekozen periode; het laatste punt vóór
  // de periode blijft staan als ankerpunt zodat de lijn niet 'in het niets' begint.
  function chartPoints(pts) {
    if (!periodDays || !pts.length) return pts;
    const cutoff = new Date(Date.now() - periodDays * 86400000).toISOString();
    const inside = pts.filter((p) => p.x >= cutoff);
    const before = pts.filter((p) => p.x < cutoff);
    return before.length ? [before[before.length - 1], ...inside] : inside;
  }

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
    const active = payload.accounts.filter((a) => !a.archived);

    // ── Totaalbanner (gearchiveerde rekeningen tellen niet mee) ─────────────
    if (totalCard && totalVal) {
      let total = 0, baseTotal = 0;
      let totalDeposits = 0, trueRendement = 0;

      active.forEach((a) => {
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
        const color = totalDelta >= 0 ? C.ok : C.err;
        html += `<span style="margin-left:12px; color:${color}; font-size:0.9em">${fmtEurDelta(totalDelta)} (${fmtPct(totalPct)}) ${esc(baselineLabel)}</span>`;
      }

      if (totalDeposits > 0) {
        const color = trueRendement >= 0 ? C.ok : C.err;
        html += `<br><small style="color:${C.muted}">Totale inleg: ${fmtEur(totalDeposits)} &nbsp;—&nbsp; `;
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
      if (acc.archived) card.style.opacity = "0.6";

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
        const color = growthDelta >= 0 ? C.ok : C.err;
        const arrow = growthDelta >= 0 ? "📈" : "📉";
        groeiBadge = `<span style="font-size:0.8em; color:${color}; font-weight:600; margin-left:8px">${arrow} ${fmtEurDelta(growthDelta)} (${fmtPct(growthPct)}) ${esc(baselineLabel)}</span>`;
      }

      let rendBadge = "";
      if (trueRendement !== null && totalDeposits !== null) {
        const color = trueRendement >= 0 ? C.ok : C.err;
        rendBadge = `<div style="font-size:0.8em; color:${C.muted}; margin-top:2px">
          Inleg: ${fmtEur(totalDeposits)} &nbsp;|&nbsp;
          Echt rendement: <strong style="color:${color}">${fmtEurDelta(trueRendement)}${trueRendPct !== null ? ` (${fmtPct(trueRendPct)})` : ""}</strong>
        </div>`;
      }

      const archiveCtl = acc.archived
        ? `<span style="font-size:0.75em; color:${C.muted2}; margin-left:8px">📦 gearchiveerd</span>
           <form method="post" action="/accounts/${esc(acc.account_number)}/unarchive" style="display:inline; margin-left:6px">
             <button type="submit" style="font-size:11px; padding:2px 8px">Herstellen</button>
           </form>`
        : `<form method="post" action="/accounts/${esc(acc.account_number)}/archive" style="display:inline; margin-left:8px"
                 onsubmit="return confirm('Rekening ${esc(acc.label)} archiveren?\\n\\nDe data blijft bewaard, maar de rekening verdwijnt van het dashboard en telt niet meer mee in totalen, API en meldingen.')">
             <button type="submit" title="Archiveren (bijv. na opheffen)" style="font-size:11px; padding:2px 8px; color:${C.muted2}">📦</button>
           </form>`;

      header.innerHTML = `
        <div>
          <div>
            <strong style="font-size:1.05em">${esc(acc.label)}</strong>
            <small style="color:${C.muted2}; margin-left:6px">${esc(acc.account_number)}</small>
            ${groeiBadge}
            ${archiveCtl}
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
          const color    = delta === null ? "" : delta >= 0 ? `color:${C.ok}` : `color:${C.err}`;
          return `<tr>
            <td style="color:${C.muted}; padding-right:12px">${esc(fmtTs(c.ts))}</td>
            <td style="font-weight:600; text-align:right">${fmtEur(c.value)}</td>
            <td style="text-align:right; padding-left:10px; ${color}">
              ${delta !== null ? fmtEurDelta(delta) : `<span style="color:${C.faint}">—</span>`}
            </td>
            <td style="text-align:right; padding-left:6px; ${color}">
              ${deltaPct !== null ? `(${fmtPct(deltaPct)})` : ""}
            </td>
            <td style="text-align:right; padding-left:8px; width:24px">
              <button type="button" class="del-point" title="Datapunt verwijderen"
                      data-acc="${esc(acc.account_number)}" data-ts="${esc(c.ts)}"
                      data-info="${esc(fmtTs(c.ts))} — ${esc(fmtEur(c.value))}"
                      style="background:none; border:none; cursor:pointer;
                             color:${C.err}; padding:2px 4px; font-size:13px">✕</button>
            </td>
          </tr>`;
        }).join("");

        tbl.innerHTML = `
          <strong style="display:block; margin-bottom:6px; color:${C.text}">Wijzigingen</strong>
          <table style="width:100%">
            <thead><tr>
              <th style="text-align:left; color:${C.muted2}; font-weight:normal">Tijdstip</th>
              <th style="text-align:right; color:${C.muted2}; font-weight:normal">Saldo</th>
              <th style="text-align:right; color:${C.muted2}; font-weight:normal" colspan="2">Δ t.o.v. vorig</th>
              <th></th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
          ${acc.first_ts && acc.first_value !== null
            ? `<p style="margin:8px 0 0; color:${C.muted2}; font-size:11px">Eerste meting: ${esc(fmtTs(acc.first_ts))} — ${fmtEur(acc.first_value)}</p>`
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
            data:            chartPoints(acc.points),
            parsing:         { xAxisKey: "x", yAxisKey: "y" },
            tension:         0.3,
            pointRadius:     3,
            borderColor:     C.accent,
            backgroundColor: C.fill,
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
              ticks: { maxTicksLimit: 6, color: C.muted2 },
              grid:  { color: C.grid },
            },
            y: {
              ticks: { callback: (v) => fmtEur(v), maxTicksLimit: 5, color: C.muted2 },
              grid:  { color: C.grid },
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
