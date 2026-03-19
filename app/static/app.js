(function () {
  const payload = window.__PAYLOAD__;
  const root    = document.getElementById("charts");

  if (!payload.accounts.length) {
    root.innerHTML = `<div class="card">Nog geen data. Ga naar Config, sla credentials op en klik op "↻ Refresh nu".</div>`;
    return;
  }

  // ── Totaalbanner ──────────────────────────────────────────────────────────
  const totalCard = document.getElementById("totaal-card");
  const totalVal  = document.getElementById("totaal-value");

  if (totalCard && totalVal) {
    const total          = payload.accounts.reduce((s, a) => s + (a.current ?? 0), 0);
    const totalDelta     = payload.accounts.reduce((s, a) => s + (a.total_delta ?? 0), 0);
    const firstTotal     = payload.accounts.reduce((s, a) => s + (a.first_value ?? 0), 0);
    const totalDeposits  = payload.accounts.reduce((s, a) => s + (a.total_deposits ?? 0), 0);
    const totalPct       = firstTotal ? (totalDelta / firstTotal * 100) : null;
    const trueRendement  = payload.accounts.reduce((s, a) => s + (a.true_rendement ?? 0), 0);
    const truePct        = totalDeposits ? (trueRendement / totalDeposits * 100) : null;

    let html = `<strong>Totaal vermogen:</strong> <span style="font-size:1.4em; font-weight:700; margin-left:8px">${fmtEur(total)}</span>`;

    if (totalDelta !== 0 && firstTotal) {
      const sign  = totalDelta >= 0 ? "+" : "";
      const color = totalDelta >= 0 ? "#0a7a0a" : "#b00020";
      html += `<span style="margin-left:12px; color:${color}; font-size:0.9em">${sign}${fmtEur(totalDelta)} (${sign}${totalPct.toFixed(2)}%) groei</span>`;
    }

    if (totalDeposits > 0) {
      const sign  = trueRendement >= 0 ? "+" : "";
      const color = trueRendement >= 0 ? "#0a7a0a" : "#b00020";
      html += `<br><small style="color:#666">Totale inleg: ${fmtEur(totalDeposits)} &nbsp;—&nbsp; `;
      html += `Echt rendement: <strong style="color:${color}">${sign}${fmtEur(trueRendement)}`;
      if (truePct !== null) html += ` (${sign}${truePct.toFixed(2)}%)`;
      html += `</strong></small>`;
    }

    totalVal.innerHTML = html;
    totalCard.style.display = "";
  }

  // ── Per-rekening kaarten ──────────────────────────────────────────────────
  payload.accounts.forEach((acc) => {
    const card = document.createElement("div");
    card.className = "card";

    const current        = acc.current          ?? null;
    const changes        = acc.changes          ?? [];
    const totalDelta     = acc.total_delta      ?? null;
    const totalDeltaPct  = acc.total_delta_pct  ?? null;
    const firstValue     = acc.first_value      ?? null;
    const firstTs        = acc.first_ts         ?? null;
    const totalDeposits  = acc.total_deposits   ?? null;
    const trueRendement  = acc.true_rendement   ?? null;
    const trueRendPct    = acc.true_rendement_pct ?? null;

    // ── Header ──
    const header = document.createElement("div");
    header.style.cssText = "display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:8px; margin-bottom:10px";

    let groeiBadge = "";
    if (totalDelta !== null && totalDeltaPct !== null) {
      const sign  = totalDelta >= 0 ? "+" : "";
      const color = totalDelta >= 0 ? "#0a7a0a" : "#b00020";
      const arrow = totalDelta >= 0 ? "📈" : "📉";
      groeiBadge = `<span style="font-size:0.8em; color:${color}; font-weight:600; margin-left:8px">${arrow} ${sign}${fmtEur(totalDelta)} (${sign}${totalDeltaPct.toFixed(2)}%) groei</span>`;
    }

    let rendBadge = "";
    if (trueRendement !== null && totalDeposits !== null) {
      const sign  = trueRendement >= 0 ? "+" : "";
      const color = trueRendement >= 0 ? "#0a7a0a" : "#b00020";
      rendBadge = `<div style="font-size:0.8em; color:#555; margin-top:2px">
        Inleg: ${fmtEur(totalDeposits)} &nbsp;|&nbsp;
        Echt rendement: <strong style="color:${color}">${sign}${fmtEur(trueRendement)}${trueRendPct !== null ? ` (${sign}${trueRendPct.toFixed(2)}%)` : ""}</strong>
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
        const sign     = delta !== null && delta >= 0 ? "+" : "";
        const color    = delta === null ? "" : delta >= 0 ? "color:#0a7a0a" : "color:#b00020";
        return `<tr>
          <td style="color:#555; padding-right:12px">${esc(fmtTs(c.ts))}</td>
          <td style="font-weight:600; text-align:right">${fmtEur(c.value)}</td>
          <td style="text-align:right; padding-left:10px; ${color}">
            ${delta !== null ? `${sign}${fmtEur(delta)}` : '<span style="color:#ccc">—</span>'}
          </td>
          <td style="text-align:right; padding-left:6px; ${color}">
            ${deltaPct !== null ? `(${sign}${deltaPct.toFixed(2)}%)` : ""}
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
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
        ${firstTs && firstValue !== null
          ? `<p style="margin:8px 0 0; color:#888; font-size:11px">Eerste meting: ${esc(fmtTs(firstTs))} — ${fmtEur(firstValue)}</p>`
          : ""}
      `;
      card.appendChild(tbl);
    }

    root.appendChild(card);

    // ── Chart.js ──
    new Chart(canvas.getContext("2d"), {
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
    });
  });

  // ── Helpers ───────────────────────────────────────────────────────────────
  function fmtEur(v) {
    if (v == null) return "—";
    return "€\u00a0" + Number(v).toLocaleString("nl-NL", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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
