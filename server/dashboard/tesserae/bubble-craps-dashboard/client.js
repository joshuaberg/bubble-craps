// Bubble Craps Dashboard — Tesserae client renderer
// Palette: extended (Spectra 6 optimized — flat colors, no gradients)

const FELT_GREEN = "#D6E8D0";
const DARK_GREEN = "#2A5C2A";
const BLACK = "#000000";
const WHITE = "#FFFFFF";
const RED = "#D0312D";
const YELLOW = "#E8C523";
const MUTED = "#555555";
const BORDER_LIGHT = "#AACCA0";

// Pip positions on a 3x3 grid (row, col)
const PIP_MAP = {
  1: [[1,1]],
  2: [[0,2],[2,0]],
  3: [[0,2],[1,1],[2,0]],
  4: [[0,0],[0,2],[2,0],[2,2]],
  5: [[0,0],[0,2],[1,1],[2,0],[2,2]],
  6: [[0,0],[0,2],[1,0],[1,2],[2,0],[2,2]],
};

function dieSvg(value, size) {
  const s = size;
  const pad = s * 0.15;
  const pip_r = s * 0.09;
  const cellW = (s - 2 * pad) / 3;

  if (!value) {
    return `<svg width="${s}" height="${s}" viewBox="0 0 ${s} ${s}">
      <rect x="2" y="2" width="${s-4}" height="${s-4}" rx="${s*0.12}" fill="#bbb" stroke="${YELLOW}" stroke-width="3"/>
      <text x="${s/2}" y="${s/2+s*0.12}" text-anchor="middle" font-size="${s*0.4}" font-weight="700" fill="#888">?</text>
    </svg>`;
  }

  const positions = PIP_MAP[value] || [];
  const pips = positions.map(([r, c]) => {
    const cx = pad + cellW * c + cellW / 2;
    const cy = pad + cellW * r + cellW / 2;
    return `<circle cx="${cx}" cy="${cy}" r="${pip_r}" fill="${BLACK}"/>`;
  }).join("");

  return `<svg width="${s}" height="${s}" viewBox="0 0 ${s} ${s}">
    <rect x="2" y="2" width="${s-4}" height="${s-4}" rx="${s*0.12}" fill="${WHITE}" stroke="${YELLOW}" stroke-width="3"/>
    ${pips}
  </svg>`;
}

function formatTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const h = d.getHours().toString().padStart(2, "0");
  const m = d.getMinutes().toString().padStart(2, "0");
  return `${h}:${m}`;
}

export default function render(shadow, ctx) {
  const { data } = ctx;

  if (data.error) {
    shadow.innerHTML = `
      <style>
        :host { display:flex; align-items:center; justify-content:center; font-family:inherit; background:${FELT_GREEN}; }
        .err { color:${RED}; text-align:center; font-size:1.5rem; font-weight:700; }
      </style>
      <div class="err">${data.error}</div>`;
    return;
  }

  const rolls = data.rolls || [];
  const stats = data.stats || {};
  const distribution = data.distribution || [];
  const lastRoll = rolls[0] || null;

  // Build distribution map (2-12)
  const distCounts = {};
  for (let t = 2; t <= 12; t++) distCounts[t] = 0;
  distribution.forEach(d => { distCounts[d.total] = d.count; });
  const maxCount = Math.max(...Object.values(distCounts), 1);

  // Recent rolls rows
  const rollRows = rolls.map(r => {
    const isSeven = r.total === 7;
    const isNull = r.roll_status !== "ok";
    const rowClass = isSeven ? "seven" : isNull ? "null-roll" : "";
    const totalClass = isSeven ? "total seven-total" : "total";
    return `<div class="roll-row ${rowClass}">
      <span>${r.die1 ?? "\u2014"}</span>
      <span>${r.die2 ?? "\u2014"}</span>
      <span class="${totalClass}">${r.total ?? "null"}</span>
      <span class="time">${formatTime(r.timestamp)}</span>
    </div>`;
  }).join("");

  // Histogram bars
  const bars = [];
  for (let t = 2; t <= 12; t++) {
    const pct = Math.max((distCounts[t] / maxCount) * 100, 1);
    const isSeven = t === 7;
    const barColor = isSeven ? RED : DARK_GREEN;
    const labelColor = isSeven ? RED : DARK_GREEN;
    const countColor = isSeven ? RED : BLACK;
    bars.push(`<div class="bar-group">
      <div class="bar-count" style="color:${countColor}">${distCounts[t]}</div>
      <div class="bar" style="height:${pct}%;background:${barColor}"></div>
      <div class="bar-label" style="color:${labelColor}">${t}</div>
    </div>`);
  }

  const lastTotalColor = lastRoll && lastRoll.total === 7 ? RED : BLACK;

  shadow.innerHTML = `
    <style>
      @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Oswald:wght@400;600;700&family=Roboto+Mono:wght@400;700&display=swap');

      :host {
        font-family: "Oswald", sans-serif;
        display: block;
        width: 100%;
        height: 100%;
      }

      .dashboard {
        display: flex;
        flex-direction: column;
        width: 100%;
        height: 100%;
        background: ${FELT_GREEN};
        color: ${BLACK};
      }

      /* Header */
      .header {
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 14px 40px;
        background: ${DARK_GREEN};
        border-bottom: 4px solid ${YELLOW};
      }
      .header-title {
        font-family: "Playfair Display", serif;
        font-size: 52px;
        font-weight: 900;
        text-transform: uppercase;
        letter-spacing: 14px;
        color: ${YELLOW};
      }
      .header-diamond {
        color: ${RED};
        font-size: 24px;
        margin: 0 28px;
      }

      /* Top 3-column section */
      .top-section {
        display: flex;
        flex: 0 0 660px;
        border-bottom: 3px solid ${DARK_GREEN};
      }

      .col {
        display: flex;
        flex-direction: column;
        justify-content: center;
        align-items: center;
        padding: 28px;
      }

      .col-last-roll {
        flex: 0 0 420px;
        border-right: 3px solid ${DARK_GREEN};
      }
      .col-stats {
        flex: 0 0 520px;
        border-right: 3px solid ${DARK_GREEN};
        align-items: flex-start;
        justify-content: flex-start;
        padding: 28px 36px;
      }
      .col-recent {
        flex: 1;
        align-items: stretch;
        justify-content: flex-start;
        padding: 20px 28px;
      }

      /* Section titles */
      .section-title {
        font-family: "Oswald", sans-serif;
        font-size: 28px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 5px;
        color: ${DARK_GREEN};
        margin-bottom: 20px;
        padding-bottom: 10px;
        border-bottom: 2px solid ${DARK_GREEN};
        width: 100%;
        text-align: center;
      }
      .col-stats .section-title { text-align: left; }

      /* Last Roll */
      .dice-row { display: flex; gap: 28px; margin-bottom: 20px; margin-top: 8px; }
      .last-total {
        font-family: "Playfair Display", serif;
        font-size: 120px;
        font-weight: 900;
        margin-top: 8px;
      }
      .last-time {
        font-family: "Roboto Mono", monospace;
        font-size: 22px;
        color: ${MUTED};
        margin-top: 4px;
      }

      /* Stats */
      .stat-row {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        width: 100%;
        padding: 16px 0;
        border-bottom: 1px solid ${BORDER_LIGHT};
      }
      .stat-label {
        font-size: 30px;
        font-weight: 400;
        color: ${DARK_GREEN};
        text-transform: uppercase;
        letter-spacing: 1px;
      }
      .stat-value {
        font-family: "Roboto Mono", monospace;
        font-size: 42px;
        font-weight: 700;
        color: ${BLACK};
      }
      .stat-value.red { color: ${RED}; }
      .stat-value.green { color: ${DARK_GREEN}; }

      /* Recent Rolls */
      .recent-header, .roll-row {
        display: grid;
        grid-template-columns: 1fr 1fr 1.2fr 1.6fr;
        text-align: center;
        padding: 8px 4px;
      }
      .recent-header {
        font-size: 20px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 2px;
        color: ${DARK_GREEN};
        border-bottom: 2px solid ${DARK_GREEN};
        margin-bottom: 4px;
        padding-bottom: 10px;
      }
      .roll-row {
        font-family: "Roboto Mono", monospace;
        font-size: 26px;
        border-bottom: 1px solid ${BORDER_LIGHT};
        padding: 11px 4px;
        color: ${BLACK};
      }
      .roll-row .total { font-weight: 700; }
      .roll-row .seven-total { color: ${RED}; }
      .roll-row .time { font-size: 20px; color: ${MUTED}; }
      .roll-row.null-roll { color: #999; font-style: italic; }

      /* Histogram */
      .bottom-section {
        flex: 1;
        display: flex;
        flex-direction: column;
        padding: 16px 60px 24px;
      }
      .bottom-section .section-title { margin-bottom: 12px; }
      .histogram {
        flex: 1;
        display: flex;
        align-items: flex-end;
        justify-content: center;
        gap: 12px;
        padding-bottom: 40px;
      }
      .bar-group {
        display: flex;
        flex-direction: column;
        align-items: center;
        flex: 1;
        max-width: 100px;
        height: 100%;
        justify-content: flex-end;
      }
      .bar {
        width: 70%;
        min-height: 3px;
        border-radius: 3px 3px 0 0;
      }
      .bar-count {
        font-family: "Roboto Mono", monospace;
        font-size: 20px;
        font-weight: 700;
        margin-bottom: 6px;
      }
      .bar-label {
        margin-top: 10px;
        font-size: 24px;
        font-weight: 700;
      }
    </style>

    <div class="dashboard">
      <div class="header">
        <span class="header-diamond">&#9830;</span>
        <span class="header-title">Bubble Craps</span>
        <span class="header-diamond">&#9830;</span>
      </div>

      <div class="top-section">
        <div class="col col-last-roll">
          <div class="section-title">Last Roll</div>
          <div class="dice-row">
            ${dieSvg(lastRoll ? lastRoll.die1 : null, 140)}
            ${dieSvg(lastRoll ? lastRoll.die2 : null, 140)}
          </div>
          <div class="last-total" style="color:${lastTotalColor}">
            ${lastRoll && lastRoll.total ? lastRoll.total : "\u2014"}
          </div>
          <div class="last-time">${lastRoll ? formatTime(lastRoll.timestamp) : ""}</div>
        </div>

        <div class="col col-stats">
          <div class="section-title">Stats</div>
          <div class="stat-row">
            <span class="stat-label">Total Rolls</span>
            <span class="stat-value">${stats.total_rolls ?? "\u2014"}</span>
          </div>
          <div class="stat-row">
            <span class="stat-label">Successful</span>
            <span class="stat-value green">${stats.successful_rolls ?? "\u2014"}</span>
          </div>
          <div class="stat-row">
            <span class="stat-label">Null Rolls</span>
            <span class="stat-value">${stats.failed_rolls ?? "\u2014"}</span>
          </div>
          <div class="stat-row">
            <span class="stat-label">Rolls Since 7</span>
            <span class="stat-value red">${stats.rolls_since_seven ?? "\u2014"}</span>
          </div>
          <div class="stat-row">
            <span class="stat-label">Avg Between 7s</span>
            <span class="stat-value">${stats.avg_between_sevens ?? "\u2014"}</span>
          </div>
        </div>

        <div class="col col-recent">
          <div class="section-title">Recent Rolls</div>
          <div class="recent-header">
            <span>D1</span>
            <span>D2</span>
            <span>Total</span>
            <span>Time</span>
          </div>
          ${rollRows}
        </div>
      </div>

      <div class="bottom-section">
        <div class="section-title">Distribution</div>
        <div class="histogram">${bars.join("")}</div>
      </div>
    </div>`;
}
