// race-chart.js
// A small, dependency-light (D3 only) animated bar-chart-race renderer.
// Used for standings-over-time and scoring-race-over-time. Generic enough
// for any "ranked entities over time" CSV with a color/icon per row.

function sanitizeId(label) {
  return "id" + String(label).replace(/[^a-zA-Z0-9]/g, "_");
}

/**
 * Builds a d3.csv row-conversion function that coerces ONLY the given
 * numeric field names to Number, leaving everything else (dates, labels,
 * team names, colors, icons) as plain strings.
 *
 * This matters more than it looks: d3.csv's built-in `d3.autoType` helper
 * auto-detects and parses ISO-format date strings into JS Date objects.
 * That silently breaks two things here -- sorting Date objects with a
 * plain `.sort()` compares their string form (e.g. "Mon Sep 15 2025..."),
 * which has nothing to do with chronological order, and a Set of Date
 * objects doesn't de-duplicate equal calendar days since each parsed cell
 * gets its own object instance. Keeping dates as plain "YYYY-MM-DD"
 * strings sorts and de-duplicates correctly with no extra code.
 */
function numericRowConverter(numericFields) {
  return function (row) {
    for (const f of numericFields) {
      if (row[f] !== undefined && row[f] !== "") row[f] = +row[f];
    }
    return row;
  };
}

/**
 * Builds one frame per distinct date in `raw`, for a given rank window
 * [rankOffset, rankOffset + rankCount) determined by each entity's value on
 * the FINAL date. Entities without a row on a given date carry forward
 * their last known value AND last known team/icon/color -- this is what
 * makes a mid-season trade show the new team from the trade date onward
 * without needing to touch historical rows.
 *
 * extraFields (optional): map of {outputKey: csvColumnName} for values that
 * should be tracked/forward-filled alongside the main value but aren't
 * used for ranking -- e.g. goals+assists tracked alongside points, so a
 * "points" bar can also show its goals/assists breakdown.
 */
function computeFrames(raw, {
  dateField, labelField, displayField, valueField, teamField, iconField, colorField,
  extraFields, rankOffset, rankCount,
}) {
  const dates = Array.from(new Set(raw.map((d) => d[dateField]))).sort();

  const byLabel = d3.group(raw, (d) => d[labelField]);
  const totalEntities = byLabel.size;

  const finalTotals = Array.from(byLabel, ([label, rows]) => ({
    label,
    value: rows[rows.length - 1][valueField],
  }))
    .sort((a, b) => d3.descending(a.value, b.value))
    .slice(rankOffset, rankOffset + rankCount)
    .map((d) => d.label);

  const extraKeys = extraFields ? Object.keys(extraFields) : [];

  const state = new Map(
    finalTotals.map((label) => {
      const s = { value: 0, display: label, team: null, icon: null, color: "#6B7684" };
      for (const k of extraKeys) s[k] = 0;
      return [label, s];
    })
  );

  const frames = dates.map((date) => {
    for (const label of finalTotals) {
      const rows = byLabel.get(label) || [];
      const match = rows.find((r) => r[dateField] === date);
      if (match) {
        const s = state.get(label);
        s.value = match[valueField];
        if (displayField) s.display = match[displayField];
        if (teamField) s.team = match[teamField];
        if (iconField) s.icon = match[iconField];
        if (colorField) s.color = match[colorField] || s.color;
        for (const k of extraKeys) s[k] = match[extraFields[k]];
      }
    }
    const bars = finalTotals
      .map((label) => {
        const s = state.get(label);
        const bar = { label, display: s.display, value: s.value, team: s.team, icon: s.icon, color: s.color };
        for (const k of extraKeys) bar[k] = s[k];
        return bar;
      })
      .sort((a, b) => d3.descending(a.value, b.value));
    return { date, bars };
  });

  return { frames, totalEntities };
}

async function renderBarChartRace(containerId, csvPath, opts) {
  const {
    dateField,
    labelField,
    displayField = null,
    valueField: staticValueField,
    iconField = null,
    teamField = null,
    colorField = null,
    seasonKeyField = null,
    seasonTypeField = null,
    metricOptions = null, // e.g. [{key:'points', label:'Points', field:'cumulative_points', breakdown:{goals:'cumulative_goals', assists:'cumulative_assists'}}, ...]
    defaultMetric = null,
    numericFields = [],
    topN = 10,
    title = "",
    stepDurationMs = 900,
  } = opts;

  let rankOffset = 0;
  let rankCount = topN;
  let selectedSeasonKey = null;
  let selectedSeasonType = "regular";
  let selectedMetricKey = defaultMetric || (metricOptions ? metricOptions[0].key : null);

  const hasSeasonSelector = !!(seasonKeyField && seasonTypeField);
  const hasMetricSelector = !!(metricOptions && metricOptions.length > 1);

  function currentMetric() {
    if (!metricOptions) return { field: staticValueField, breakdown: null };
    return metricOptions.find((m) => m.key === selectedMetricKey) || metricOptions[0];
  }

  const container = document.getElementById(containerId);
  container.innerHTML = `
    <div class="race-header">
      <h2>${title}</h2>
      <div class="race-controls">
        ${hasSeasonSelector ? `
        <label class="race-season-label">Season
          <select class="race-season-select"></select>
        </label>
        <label class="race-season-label">
          <select class="race-type-select">
            <option value="regular">Regular Season</option>
            <option value="playoffs">Playoffs</option>
          </select>
        </label>` : ""}
        ${hasMetricSelector ? `
        <label class="race-season-label">
          <select class="race-metric-select">
            ${metricOptions.map((m) => `<option value="${m.key}">${m.label}</option>`).join("")}
          </select>
        </label>` : ""}
        <label class="race-topn-label">Show
          <input type="number" class="race-topn-input" min="1" max="50" value="${topN}">
        </label>
        <button class="race-page-btn race-page-prev" aria-label="Previous">‹</button>
        <span class="race-page-label"></span>
        <button class="race-page-btn race-page-next" aria-label="Next">›</button>
      </div>
    </div>
    <div class="race-controls race-controls-playback">
      <button class="race-play-btn" aria-label="Play">▶</button>
      <input type="range" class="race-scrubber" min="0" value="0" step="1">
      <span class="race-date-label"></span>
    </div>
    <div class="race-svg-wrap">
      <svg class="race-svg"></svg>
      <p class="race-empty" style="display:none">No data for this season/type yet.</p>
    </div>
  `;

  const allRows = await d3.csv(csvPath, numericRowConverter(numericFields));
  if (!allRows.length) {
    container.querySelector(".race-svg-wrap").innerHTML =
      "<p class='race-empty'>No data yet — check back once games have been played.</p>";
    return;
  }

  const emptyMsgEl = container.querySelector(".race-svg-wrap > .race-empty");
  const svgEl = container.querySelector(".race-svg");

  const seasonSelect = container.querySelector(".race-season-select");
  const typeSelect = container.querySelector(".race-type-select");
  const metricSelect = container.querySelector(".race-metric-select");
  const topnInput = container.querySelector(".race-topn-input");
  const pagePrevBtn = container.querySelector(".race-page-prev");
  const pageNextBtn = container.querySelector(".race-page-next");
  const pageLabelEl = container.querySelector(".race-page-label");
  const dateLabelEl = container.querySelector(".race-date-label");
  const scrubber = container.querySelector(".race-scrubber");
  const playBtn = container.querySelector(".race-play-btn");

  if (hasSeasonSelector) {
    const seasonKeys = Array.from(new Set(allRows.map((d) => d[seasonKeyField]))).sort().reverse();
    selectedSeasonKey = seasonKeys[0] || null;
    seasonSelect.innerHTML = seasonKeys.map((k) => `<option value="${k}">${k}</option>`).join("");
    seasonSelect.value = selectedSeasonKey;
    typeSelect.value = selectedSeasonType;
  }
  if (hasMetricSelector) {
    metricSelect.value = selectedMetricKey;
  }

  function currentRaw() {
    if (!hasSeasonSelector) return allRows;
    return allRows.filter(
      (d) => d[seasonKeyField] === selectedSeasonKey && d[seasonTypeField] === selectedSeasonType
    );
  }

  const margin = { top: 10, right: 90, bottom: 10, left: 150 };
  const barGap = 8;
  const width = container.clientWidth || 640;

  const svg = d3.select(container.querySelector(".race-svg")).attr("width", "100%");
  const gBars = svg.append("g");
  const gIcons = svg.append("g");
  const gLabels = svg.append("g").attr("font-size", 13);
  const gValues = svg.append("g").attr("font-size", 13).attr("text-anchor", "start");
  const defs = svg.append("defs");

  const x = d3.scaleLinear().range([margin.left, width - margin.right]);
  let y = d3.scaleBand();
  let frames = [];
  let currentFrame = 0;
  let playing = false;
  let timer = null;

  function stop() {
    playing = false;
    playBtn.textContent = "▶";
    if (timer) clearInterval(timer);
  }

  function formatValue(d) {
    const metric = currentMetric();
    if (metric.breakdown) {
      return `${d.value} (${d.goals}G, ${d.assists}A)`;
    }
    return String(d.value);
  }

  function draw(frameIndex) {
    const frame = frames[frameIndex];
    const barHeight = 32;
    const n = rankCount;
    const height = margin.top + margin.bottom + n * (barHeight + barGap);
    svg.attr("viewBox", [0, 0, width, height]).attr("height", height);
    y = d3
      .scaleBand()
      .domain(d3.range(n))
      .range([margin.top, margin.top + n * (barHeight + barGap)])
      .padding(0.15);

    const top = frame.bars.slice(0, n);
    x.domain([0, d3.max(top, (d) => d.value) || 1]).nice();

    // per-label clip circles for logos, created once per label as needed
    for (const d of top) {
      const cid = sanitizeId(d.label);
      if (defs.select(`#clip-${cid}`).empty()) {
        defs
          .append("clipPath")
          .attr("id", `clip-${cid}`)
          .append("circle")
          .attr("r", 11);
      }
    }

    const bars = gBars.selectAll("rect").data(top, (d) => d.label);
    bars
      .join(
        (enter) =>
          enter
            .append("rect")
            .attr("x", x(0))
            .attr("y", (d, i) => y(i))
            .attr("height", y.bandwidth())
            .attr("width", 0),
        (update) => update,
        (exit) => exit.remove()
      )
      .transition()
      .duration(stepDurationMs)
      .ease(d3.easeCubicOut)
      .attr("y", (d, i) => y(i))
      .attr("width", (d) => Math.max(0, x(d.value) - x(0)))
      .attr("fill", (d) => d.color || "#6B7684");

    const icons = gIcons.selectAll("image").data(
      top.filter((d) => d.icon),
      (d) => d.label
    );
    icons
      .join(
        (enter) =>
          enter
            .append("image")
            .attr("href", (d) => d.icon)
            .attr("width", 22)
            .attr("height", 22)
            .attr("clip-path", (d) => `url(#clip-${sanitizeId(d.label)})`),
        (update) => update,
        (exit) => exit.remove()
      )
      .transition()
      .duration(stepDurationMs)
      .ease(d3.easeCubicOut)
      .attr("x", (d, i) => x(d.value) - 26)
      .attr("y", (d, i) => y(i) + y.bandwidth() / 2 - 11);

    const labels = gLabels.selectAll("text").data(top, (d) => d.label);
    labels
      .join(
        (enter) =>
          enter
            .append("text")
            .attr("x", margin.left - 10)
            .attr("y", (d, i) => y(i) + y.bandwidth() / 2)
            .attr("dy", "0.35em")
            .attr("text-anchor", "end")
            .text((d) => d.display),
        (update) => update,
        (exit) => exit.remove()
      )
      .transition()
      .duration(stepDurationMs)
      .ease(d3.easeCubicOut)
      .attr("y", (d, i) => y(i) + y.bandwidth() / 2);

    const values = gValues.selectAll("text").data(top, (d) => d.label);
    values
      .join(
        (enter) =>
          enter
            .append("text")
            .attr("x", (d) => x(d.value) + 8)
            .attr("y", (d, i) => y(i) + y.bandwidth() / 2)
            .attr("dy", "0.35em")
            .text((d) => formatValue(d)),
        (update) => update,
        (exit) => exit.remove()
      )
      .transition()
      .duration(stepDurationMs)
      .ease(d3.easeCubicOut)
      .attr("x", (d) => x(d.value) + 8)
      .attr("y", (d, i) => y(i) + y.bandwidth() / 2)
      .textTween(function (d) {
        // Only animate the count-up for the plain-number case; a
        // goals/assists breakdown string isn't meaningfully "interpolated"
        // digit by digit, so it just snaps to the new value each frame.
        if (currentMetric().breakdown) {
          return () => formatValue(d);
        }
        const node = this;
        const prev = +node.__prevValue__ || 0;
        node.__prevValue__ = d.value;
        const i = d3.interpolateRound(prev, d.value);
        return (t) => i(t).toString();
      });

    dateLabelEl.textContent = frame.date;
    scrubber.max = frames.length - 1;
    scrubber.value = frameIndex;
  }

  function rebuild() {
    stop();
    const metric = currentMetric();
    const result = computeFrames(currentRaw(), {
      dateField, labelField, displayField,
      valueField: metric.field,
      teamField, iconField, colorField,
      extraFields: metric.breakdown || null,
      rankOffset, rankCount,
    });
    frames = result.frames;
    const total = result.totalEntities;
    if (!frames.length) {
      pageLabelEl.textContent = "0 of 0";
      svgEl.style.display = "none";
      emptyMsgEl.style.display = "";
      return;
    }
    svgEl.style.display = "";
    emptyMsgEl.style.display = "none";
    const from = Math.min(rankOffset + 1, total);
    const to = Math.min(rankOffset + rankCount, total);
    pageLabelEl.textContent = total ? `${from}–${to} of ${total}` : "0 of 0";
    pagePrevBtn.disabled = rankOffset === 0;
    pageNextBtn.disabled = rankOffset + rankCount >= total;
    currentFrame = frames.length - 1; // land on latest standings by default
    draw(currentFrame);
  }

  function play() {
    if (currentFrame >= frames.length - 1) currentFrame = 0;
    playing = true;
    playBtn.textContent = "⏸";
    timer = setInterval(() => {
      currentFrame += 1;
      if (currentFrame >= frames.length) {
        stop();
        return;
      }
      draw(currentFrame);
    }, stepDurationMs);
  }

  playBtn.addEventListener("click", () => (playing ? stop() : play()));
  scrubber.addEventListener("input", (e) => {
    stop();
    currentFrame = +e.target.value;
    draw(currentFrame);
  });
  topnInput.addEventListener("change", () => {
    const v = Math.max(1, Math.min(50, +topnInput.value || topN));
    topnInput.value = v;
    rankCount = v;
    rankOffset = 0; // changing page size resets to the first page
    rebuild();
  });
  pagePrevBtn.addEventListener("click", () => {
    rankOffset = Math.max(0, rankOffset - rankCount);
    rebuild();
  });
  pageNextBtn.addEventListener("click", () => {
    rankOffset = rankOffset + rankCount;
    rebuild();
  });
  if (hasSeasonSelector) {
    seasonSelect.addEventListener("change", () => {
      selectedSeasonKey = seasonSelect.value;
      rankOffset = 0;
      rebuild();
    });
    typeSelect.addEventListener("change", () => {
      selectedSeasonType = typeSelect.value;
      rankOffset = 0;
      rebuild();
    });
  }
  if (hasMetricSelector) {
    metricSelect.addEventListener("change", () => {
      selectedMetricKey = metricSelect.value;
      rankOffset = 0;
      rebuild();
    });
  }

  rebuild();
}
