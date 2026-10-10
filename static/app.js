/* Dashboard hackatonu: pobiera /api/dashboard i rotuje widoki na ekranach w sali jak slajdy.
   Parametry URL (opis w README):
     view=race             ekran do wyświetlenia (bez slide: stoi na nim bez zmian)
     slide=20              włącza rotację: czas jednego ekranu w sekundach (min. 5, zalecane 20)
     views=race,total,grid które ekrany rotują i w jakiej kolejności (domyślnie wszystkie)
     refresh=30            co ile sekund przeglądarka pobiera nowe dane */
(() => {
  const params = new URLSearchParams(location.search);
  const REFRESH_MS = (Number(params.get("refresh")) || 30) * 1000;
  const VIEWS = ["race", "loc", "total", "heat", "grid"];
  const SLIDE_MS = Math.max(5, Number(params.get("slide")) || 20) * 1000;
  const ROTATION = (params.get("views") || "").split(",").map((v) => v.trim()).filter((v) => VIEWS.includes(v));
  if (!ROTATION.length) ROTATION.push(...VIEWS);
  // odtworzenie wyścigu zajmuje do 70% czasu slajdu (maks. 15 s), resztę widać wynik końcowy
  const REPLAY_MS = Math.min(15000, SLIDE_MS * 0.7);
  const MUTED = "#8a96b3", LINE = "#1f2a44", TEXT = "#e8edf7";

  let data = null;
  let clockOffset = 0;            // różnica zegara serwera i przeglądarki
  let view = VIEWS.includes(params.get("view")) ? params.get("view") : ROTATION[0];
  let rotate = params.has("slide") && params.get("rotate") !== "0";  // bez slide ekran stoi
  let rotateTimer = null;
  let raceMetric = "commits";
  let replayTimer = null;
  let seenEvents = null;
  const toastQueue = [];

  const $ = (id) => document.getElementById(id);
  const fmt = (n) => (n ?? 0).toLocaleString("pl-PL");
  const charts = {
    race: echarts.init($("chart-race"), null, { renderer: "canvas" }),
    loc: echarts.init($("chart-loc")),
    total: echarts.init($("chart-total")),
    heat: echarts.init($("chart-heat")),
  };
  window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));

  // ---------- dane ----------
  async function load() {
    try {
      const res = await fetch("/api/dashboard", { cache: "no-store" });
      if (!res.ok) throw new Error(res.status);
      const next = await res.json();
      clockOffset = Date.parse(next.meta.now) - Date.now();
      const first = !data;
      data = next;
      renderHeader();
      renderKpis(first);
      renderSide();
      renderTicker();
      if (first) showView(view, true);
      else refreshCurrent();
    } catch (e) {
      $("updated").textContent = "Brak połączenia z serwerem, pokazuję ostatnie dane";
      $("updated").classList.add("stale");
    }
  }

  // ---------- nagłówek i zegar ----------
  function renderHeader() {
    const m = data.meta;
    document.title = `${m.event_name} · Live`;
    $("demo-badge").hidden = !m.demo;
    const upd = $("updated");
    if (m.last_success_at) {
      const t = new Date(m.last_success_at);
      const ageMin = (Date.now() + clockOffset - t) / 60000;
      upd.textContent = `Dane z GitHuba: ${t.toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" })}`;
      upd.classList.toggle("stale", ageMin > (m.poll_interval / 60) * 2.5);
    } else {
      upd.textContent = "Czekam na pierwszą synchronizację…";
    }
  }

  function tick() {
    if (!data) return;
    const now = Date.now() + clockOffset;
    const start = Date.parse(data.meta.start), end = Date.parse(data.meta.end);
    let label = "Do końca", ms = end - now;
    if (now < start) { label = "Do startu"; ms = start - now; }
    if (now >= end) { label = "Koniec hackatonu"; ms = 0; }
    const s = Math.max(0, Math.floor(ms / 1000));
    const hh = String(Math.floor(s / 3600)).padStart(2, "0");
    const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    $("clock-label").textContent = label;
    $("countdown").textContent = `${hh}:${mm}:${ss}`;
    const pct = Math.min(100, Math.max(0, ((now - start) / (end - start)) * 100));
    $("progress-bar").style.width = `${pct}%`;
  }
  setInterval(tick, 1000);

  // ---------- KPI ----------
  function setKpi(id, value, first) {
    const el = $(id);
    const prev = Number(el.dataset.v || 0);
    if (first) { animateNumber(el, 0, value, 1500); }
    else if (prev !== value) { animateNumber(el, prev, value, 1200); el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump"); }
    el.dataset.v = value;
  }
  function animateNumber(el, from, to, ms) {
    const t0 = performance.now();
    const step = (t) => {
      const p = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - p, 3);
      el.textContent = fmt(Math.round(from + (to - from) * e));
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  function renderKpis(first) {
    const t = data.totals;
    setKpi("k-commits", t.commits, first);
    setKpi("k-loc", t.loc_net, first);
    setKpi("k-prs", t.prs_merged, first);
    setKpi("k-rate", t.commits_1h, first);
    setKpi("k-teams", t.active_teams_1h, first);
    setKpi("k-authors", t.authors_1h, first);
  }

  // ---------- panel boczny: najgorętszy zespół i wydarzenia ----------
  const timeOf = (iso) => new Date(iso).toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
  const escapeHtml = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function renderSide() {
    const hot = data.hottest;
    $("hot-name").textContent = hot ? hot.name : "—";
    $("hot-stats").textContent = hot ? `${hot.commits_1h} commitów · ${fmt(hot.lines_1h)} linii w ostatniej godzinie` : "Czekamy na pierwsze commity";

    const keyOf = (e) => `${e.t}|${e.text}`;
    const fresh = seenEvents ? data.events.filter((e) => !seenEvents.has(keyOf(e))) : [];
    seenEvents = new Set(data.events.map(keyOf));

    $("feed").innerHTML = data.events.slice(0, 16).map((e) =>
      `<li class="${e.type}"><time>${timeOf(e.t)}</time><span>${escapeHtml(e.text)}</span></li>`).join("");

    fresh.filter((e) => e.type === "milestone" || e.type === "rank").slice(0, 3).forEach((e) => toastQueue.push(e.text));
    if (toastQueue.length) showToast();
  }

  function showToast() {
    const el = $("toast");
    if (!el.hidden || !toastQueue.length) return;
    el.textContent = toastQueue.shift();
    el.hidden = false;
    el.style.animation = "none"; void el.offsetWidth; el.style.animation = "";
    setTimeout(() => { el.hidden = true; showToast(); }, 4000);
  }

  function renderTicker() {
    const top = data.teams.slice(0, 5).map((t) => `${t.rank}. <b>${escapeHtml(t.name)}</b> ${fmt(t.commits)}`).join(" · ");
    const evs = data.events.slice(0, 12).map((e) => `${timeOf(e.t)} <b>${escapeHtml(e.text)}</b>`);
    $("ticker").innerHTML = [`Ranking commitów: ${top}`, ...evs].join('<span class="sep">◆</span>');
  }

  // ---------- widoki ----------
  function showView(name, initial = false) {
    view = name;
    VIEWS.forEach((v) => { $(`view-${v}`).hidden = v !== name; });
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    $("rotate-hint").textContent = rotate ? `rotacja: co ${SLIDE_MS / 1000} s (spacja = pauza)` : "ekran stały (spacja = rotacja)";
    if (charts[name]) charts[name].resize();
    if (name === "race") renderRace(true);
    if (name === "loc") renderLoc();
    if (name === "total") renderTotal();
    if (name === "heat") renderHeat();
    if (name === "grid") renderGrid();
    scheduleRotation();
  }

  function refreshCurrent() {
    if (view === "race" && !replayTimer) renderRace(false);
    if (view === "loc") renderLoc();
    if (view === "total") renderTotal();
    if (view === "heat") renderHeat();
    if (view === "grid") renderGrid();
  }

  function scheduleRotation() {
    clearTimeout(rotateTimer);
    if (!rotate) return;
    rotateTimer = setTimeout(() => {
      const pos = ROTATION.indexOf(view);
      const next = ROTATION[(pos + 1) % ROTATION.length];
      if (next === "race") raceMetric = raceMetric === "commits" ? "loc" : "commits";
      showView(next);
    }, SLIDE_MS);
  }

  // --- 1. wyścig (bar chart race z odtworzeniem przebiegu hackatonu) ---
  function raceOption(idx) {
    const s = data.series, series = s[raceMetric];
    const teams = data.teams;
    const maxBars = Math.max(8, Math.floor(($("chart-race").clientHeight || 600) / 30));
    return {
      animationDurationUpdate: 280,
      animationEasingUpdate: "linear",
      grid: { left: 190, right: 90, top: 8, bottom: 24, containLabel: false },
      xAxis: { type: "value", max: "dataMax", axisLabel: { color: MUTED, formatter: (v) => fmt(v) }, splitLine: { lineStyle: { color: LINE } } },
      yAxis: {
        type: "category", inverse: true, max: Math.min(maxBars, teams.length) - 1,
        data: teams.map((t) => t.name),
        axisLabel: { color: TEXT, fontSize: 14, fontFamily: "Space Grotesk", width: 176, overflow: "truncate" },
        axisLine: { show: false }, axisTick: { show: false },
        animationDuration: 300, animationDurationUpdate: 300,
      },
      series: [{
        type: "bar", realtimeSort: true, barCategoryGap: "22%",
        data: teams.map((t) => ({ value: series[t.repo][idx] ?? 0, itemStyle: { color: t.color, borderRadius: [0, 6, 6, 0] } })),
        label: { show: true, position: "right", valueAnimation: true, color: TEXT, fontFamily: "JetBrains Mono", fontSize: 14, formatter: (p) => fmt(p.value) },
      }],
      graphic: [{
        type: "text", right: 100, bottom: 30, z: 100,
        style: { text: s.labels[idx] || "", font: "700 44px 'JetBrains Mono'", fill: "rgba(232,237,247,.18)" },
      }],
    };
  }

  function renderRace(replay) {
    const s = data.series;
    $("race-title").textContent = raceMetric === "commits" ? "Wyścig commitów" : "Wyścig linii kodu";
    clearInterval(replayTimer); replayTimer = null;
    const last = s.times.length - 1;
    if (last < 0) return;
    if (!replay) { charts.race.setOption(raceOption(last), { replaceMerge: ["series"] }); $("race-time").textContent = "stan teraz"; return; }
    let i = 0;
    charts.race.clear();
    charts.race.setOption(raceOption(0));
    $("race-time").textContent = "odtwarzanie od startu hackatonu";
    const stepMs = Math.max(60, Math.min(350, REPLAY_MS / Math.max(1, last)));
    replayTimer = setInterval(() => {
      i += 1;
      charts.race.setOption(raceOption(Math.min(i, last)));
      if (i >= last) { clearInterval(replayTimer); replayTimer = null; $("race-time").textContent = "stan teraz"; }
    }, stepMs);
  }

  // --- 2. linie kodu w czasie ---
  function renderLoc() {
    const s = data.series;
    const top = new Set(data.teams.slice().sort((a, b) => b.loc_net - a.loc_net).slice(0, 6).map((t) => t.repo));
    charts.loc.setOption({
      animationDuration: 1200,
      grid: { left: 8, right: 210, top: 16, bottom: 8, containLabel: true },
      tooltip: { trigger: "axis", order: "valueDesc", backgroundColor: "#0e1526", borderColor: LINE, textStyle: { color: TEXT } },
      xAxis: { type: "category", data: s.labels, boundaryGap: false, axisLabel: { color: MUTED }, axisLine: { lineStyle: { color: LINE } } },
      yAxis: { type: "value", axisLabel: { color: MUTED, formatter: (v) => fmt(v) }, splitLine: { lineStyle: { color: LINE } } },
      series: data.teams.map((t) => ({
        name: t.name, type: "line", showSymbol: false, smooth: 0.3,
        data: s.loc[t.repo],
        lineStyle: { width: top.has(t.repo) ? 3.5 : 2, color: t.color, opacity: 0.95 },
        itemStyle: { color: t.color },
        emphasis: { focus: "series" },
        labelLayout: { moveOverlap: "shiftY" },
        endLabel: { show: true, color: t.color, fontFamily: "Space Grotesk", fontSize: data.teams.length > 15 ? 12 : 14,
                    fontWeight: top.has(t.repo) ? 700 : 400, formatter: (p) => `${t.name}  ${fmt(p.value)}` },
      })),
    }, true);
  }

  // --- 3. łączna liczba linii kodu całego hackatonu ---
  function renderTotal() {
    const s = data.series;
    const total = s.times.map((_, i) => data.teams.reduce((acc, t) => acc + (s.loc[t.repo][i] || 0), 0));
    const bars = data.teams.map((t) => ({
      name: t.name, type: "bar", stack: "przyrost", yAxisIndex: 1, barWidth: "70%",
      data: s.loc[t.repo].map((v, i) => (i ? v - s.loc[t.repo][i - 1] : v)),
      itemStyle: { color: t.color, opacity: 0.85 },
      emphasis: { focus: "series" },
    }));
    charts.total.setOption({
      animationDuration: 1500,
      grid: { left: 8, right: 16, top: 70, bottom: 8, containLabel: true },
      tooltip: {
        trigger: "axis", axisPointer: { type: "shadow" }, backgroundColor: "#0e1526", borderColor: LINE, textStyle: { color: TEXT },
        formatter: (ps) => {
          const line = ps.find((p) => p.seriesName === "Linie kodu łącznie");
          const top = ps.filter((p) => p.seriesName !== "Linie kodu łącznie" && p.value).sort((x, y) => y.value - x.value).slice(0, 8);
          return `<b>${ps[0].axisValue}</b><br>Łącznie: <b>${fmt(line ? line.value : 0)}</b><br>Przyrost w przedziale:<br>` +
            top.map((p) => `${p.marker}${p.seriesName}: ${fmt(p.value)}`).join("<br>");
        },
      },
      legend: { top: 0, left: 0, right: 0, type: "plain", itemWidth: 10, itemHeight: 10, itemGap: 10,
                textStyle: { color: MUTED, fontSize: 11 }, data: data.teams.map((t) => t.name) },
      xAxis: { type: "category", data: s.labels, axisLabel: { color: MUTED }, axisLine: { lineStyle: { color: LINE } } },
      yAxis: [
        { type: "value", name: "łącznie", nameTextStyle: { color: MUTED }, axisLabel: { color: MUTED, formatter: (v) => fmt(v) }, splitLine: { lineStyle: { color: LINE } } },
        { type: "value", name: "przyrost", nameTextStyle: { color: MUTED }, axisLabel: { color: MUTED, formatter: (v) => fmt(v) }, splitLine: { show: false } },
      ],
      series: [
        ...bars,
        { name: "Linie kodu łącznie", type: "line", data: total, showSymbol: false, smooth: 0.3, z: 10,
          lineStyle: { width: 4, color: "#ffffff", shadowColor: "rgba(0,0,0,.6)", shadowBlur: 8 }, itemStyle: { color: "#ffffff" },
          endLabel: { show: true, color: TEXT, fontFamily: "JetBrains Mono", fontSize: 18, fontWeight: 700,
                      formatter: (p) => fmt(p.value), offset: [-90, -24] } },
      ],
    }, true);
  }

  // --- 4. heatmapa rytmu hackatonu ---
  function renderHeat() {
    const h = data.heatmap;
    const byRepo = Object.fromEntries(data.teams.map((t) => [t.repo, t.name]));
    const max = Math.max(1, ...h.cells.map((c) => c[2]));
    charts.heat.setOption({
      grid: { left: 8, right: 16, top: 8, bottom: 60, containLabel: true },
      tooltip: { formatter: (p) => `${byRepo[h.teams[p.value[1]]]}<br>${h.hours[p.value[0]]}: <b>${p.value[2]}</b> commitów`,
                 backgroundColor: "#0e1526", borderColor: LINE, textStyle: { color: TEXT } },
      xAxis: { type: "category", data: h.hours, axisLabel: { color: MUTED, interval: Math.ceil(h.hours.length / 16) }, splitArea: { show: false }, axisLine: { lineStyle: { color: LINE } } },
      yAxis: { type: "category", data: h.teams.map((r) => byRepo[r]), inverse: true, axisLabel: { color: TEXT, fontSize: 12 }, axisLine: { show: false } },
      visualMap: { min: 0, max, calculable: false, orient: "horizontal", left: "center", bottom: 4, itemHeight: 220,
                   textStyle: { color: MUTED }, inRange: { color: ["#111a30", "#1d4e89", "#4cc9f0", "#f72585", "#ffd166"] } },
      series: [{ type: "heatmap", data: h.cells, itemStyle: { borderColor: "#0e1526", borderWidth: 2, borderRadius: 3 },
                 emphasis: { itemStyle: { borderColor: TEXT } } }],
    }, true);
  }

  // --- 5. karty zespołów ---
  function spark(values, color) {
    const w = 200, h = 36, max = Math.max(1, ...values), n = values.length - 1 || 1;
    const pts = values.map((v, i) => `${(i / n) * w},${h - 3 - (v / max) * (h - 6)}`);
    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline points="0,${h} ${pts.join(" ")} ${w},${h}" fill="${color}" fill-opacity=".15" stroke="none"/>` +
      `<polyline points="${pts.join(" ")}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>`;
  }
  function renderGrid() {
    $("team-grid").innerHTML = data.teams.map((t) => {
      const rc = t.rank_change > 0 ? `<span class="up">▲${t.rank_change}</span>` : t.rank_change < 0 ? `<span class="down">▼${-t.rank_change}</span>` : "";
      const mom = t.momentum > 0 ? `<span class="mom up">↑ ${t.momentum}</span>` : t.momentum < 0 ? `<span class="mom down">↓ ${-t.momentum}</span>` : `<span class="mom flat">→ 0</span>`;
      return `<div class="card" style="--c:${t.color}">
        <div class="card-top"><span class="card-name">${escapeHtml(t.name)}</span><span class="card-rank">#${t.rank} ${rc}</span></div>
        <div class="nums"><div><b>${fmt(t.commits)}</b><small>commitów</small></div><div><b>${fmt(t.loc_net)}</b><small>linii</small></div><div><b>${t.prs_merged}</b><small>PR</small></div></div>
        ${spark(t.spark, t.color)}
        <div class="foot"><span>1 h: <b>${t.commits_1h}</b></span>${mom}<span title="osoby kodujące w ostatniej godzinie">👥 ${t.authors_1h}</span></div>
      </div>`;
    }).join("");
  }

  // ---------- sterowanie ----------
  document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => { rotate = false; showView(b.dataset.view); }));
  document.addEventListener("keydown", (e) => {
    if (["1", "2", "3", "4", "5"].includes(e.key)) { rotate = false; showView(VIEWS[Number(e.key) - 1]); }
    if (e.key === " ") { e.preventDefault(); rotate = !rotate; showView(view); }
    if (e.key.toLowerCase() === "f") toggleFullscreen();
  });
  const toggleFullscreen = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen());
  $("fullscreen").addEventListener("click", toggleFullscreen);

  load();
  setInterval(load, REFRESH_MS);
})();
