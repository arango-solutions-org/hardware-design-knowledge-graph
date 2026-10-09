// app.js — orchestration: state, play loop, controls, wiring.
(async function () {
  const State = {
    ts: 0, tsMin: 0, tsMax: 0,
    repos: [], activeRepos: [],
    projection: 'traceability',
    repoColors: {},
    playing: false, speed: 18,
    filters: { depends: true, cross: true, provOnly: false },
    lastSliceKey: null,
  };

  // module-level mutable state (declared before boot to avoid temporal-dead-zone)
  let refreshSeq = 0, refreshInFlight = false, refreshQueued = false, lastRefreshStart = 0,
      rafId = null, lastFrame = null;
  const MIN_REFRESH_INTERVAL_MS = 100; // cap request rate even when round-trips are fast

  // ── boot ──
  const [health, reposResp, tl] = await Promise.all([API.health(), API.repos(), API.timeline()]);
  document.getElementById('sourceBadge').textContent = health.source;
  document.getElementById('sourceBadge').classList.toggle('live', health.source === 'arango');

  State.repos = reposResp.repos;
  State.repos.forEach(r => State.repoColors[r.name] = r.color || '#4f8cff');
  State.tsMin = tl.ts_min; State.tsMax = tl.ts_max; State.ts = tl.ts_max;

  // default active repos: or1200 (richest structure) + mor1kx (deep history) to show lineage
  State.activeRepos = State.repos.filter(r => ['or1200', 'mor1kx'].includes(r.name)).map(r => r.name);
  if (!State.activeRepos.length) State.activeRepos = State.repos.slice(0, 2).map(r => r.name);

  // ── init modules ──
  Graph.init('cy', { repoColors: State.repoColors, onSelect: showProvenance });
  Timeline.init('tlSvg', { repoColors: State.repoColors, onSeek: seek });
  Timeline.setData(tl);
  Timeline.setRepos(State.activeRepos);
  Inspector.init({ repoColors: State.repoColors, onRelation: (id) => { Graph.centerOn(id); showProvenance(id); } });

  buildRepoToggles();
  buildLegend(tl.epoch_colors || {});
  bindControls();

  Timeline.setPlayhead(State.ts);
  await refresh(true);

  // ── data refresh ──
  async function refresh(relayout = false) {
    if (!State.activeRepos.length) { return; }
    // whole seconds, matching what API.slice sends — play advances by fractions
    const key = `${Math.floor(State.ts)}|${State.activeRepos.join(',')}|${State.projection}`;
    if (key === State.lastSliceKey && !relayout) return;
    State.lastSliceKey = key;
    const seq = ++refreshSeq;
    showLoading(true);
    try {
      const slice = await API.slice(State.ts, State.activeRepos, State.projection);
      if (seq !== refreshSeq) return; // stale
      Graph.setData(slice, { relayout });
      Graph.setEdgeVisibility(State.filters);
      Graph.setProvenanceOnly(State.filters.provOnly);
      renderStats(slice);
      renderEpochContext(slice.epoch_context);
    } catch (e) {
      console.error(e);
    } finally {
      if (seq === refreshSeq) showLoading(false);
    }
    updateReadout();
  }

  // Coalescing refresh for scrubbing/playback: at most one /api/slice call in
  // flight at a time, spaced at least MIN_REFRESH_INTERVAL_MS apart. tick()
  // calls this every animation frame (~16ms), far faster than a typical
  // round-trip against the live cluster (measured ~200ms-1s depending on how
  // many modules are active at that point in history) — a fixed-interval
  // throttle still issues requests faster than they resolve, so the seq-based
  // staleness guard above discards nearly every response and the graph never
  // visibly updates. Coalescing fixes that: while a request is in flight, just
  // remember another was requested; when it resolves, fetch again — reading
  // State.ts fresh at that moment, so it always shows the current position
  // rather than a queued backlog of intermediate ones. The min-interval floor
  // additionally caps request rate on the other end: for early/sparse time
  // ranges the query itself is cheap and round-trips fast, and without a floor
  // coalescing alone would fire on nearly every animation frame (~60/s).
  async function refreshThrottled() {
    if (refreshInFlight) { refreshQueued = true; return; }
    refreshInFlight = true;
    do {
      refreshQueued = false;
      const wait = MIN_REFRESH_INTERVAL_MS - (performance.now() - lastRefreshStart);
      if (wait > 0) await new Promise(r => setTimeout(r, wait));
      lastRefreshStart = performance.now();
      await refresh(false);
    } while (refreshQueued);
    refreshInFlight = false;
  }

  function seek(ts) {
    State.ts = Math.max(State.tsMin, Math.min(State.tsMax, ts));
    Timeline.setPlayhead(State.ts);
    updateReadout();
    refreshThrottled();
  }

  async function showProvenance(id) {
    try { Inspector.render(await API.provenance(id)); }
    catch (e) { console.error('provenance', e); }
  }

  // ── play loop ──
  function tick(now) {
    if (!State.playing) return;
    if (lastFrame == null) lastFrame = now;
    const dt = (now - lastFrame) / 1000; lastFrame = now;
    const span = State.tsMax - State.tsMin;
    // speed maps 1..60 → traverse full span in ~(70 - speed) seconds
    const perSec = span / (72 - State.speed);
    State.ts = Math.min(State.tsMax, State.ts + perSec * dt);
    Timeline.setPlayhead(State.ts);
    updateReadout();
    refreshThrottled();
    if (State.ts >= State.tsMax) { stopPlay(); }
    else rafId = requestAnimationFrame(tick);
  }
  function startPlay() {
    if (State.ts >= State.tsMax) State.ts = State.tsMin;
    State.playing = true; lastFrame = null;
    document.getElementById('playBtn').textContent = '⏸';
    document.getElementById('playBtn').classList.add('playing');
    rafId = requestAnimationFrame(tick);
  }
  function stopPlay() {
    State.playing = false; cancelAnimationFrame(rafId);
    document.getElementById('playBtn').textContent = '▶';
    document.getElementById('playBtn').classList.remove('playing');
  }

  // ── UI builders ──
  function buildRepoToggles() {
    const box = document.getElementById('repoToggles');
    box.innerHTML = '';
    State.repos.forEach(r => {
      const on = State.activeRepos.includes(r.name);
      const chip = document.createElement('button');
      chip.className = 'repo-chip' + (on ? ' on' : '');
      chip.style.background = on ? (r.color + '22') : '';
      chip.style.borderColor = on ? r.color : '';
      chip.innerHTML = `<span class="dot" style="background:${r.color}"></span>${r.short || r.name}
        <span style="opacity:.6;font-weight:400">${r.module_version_count ? r.epoch_count + 'e' : ''}</span>`;
      chip.title = `${r.canonical}\n${r.epoch_count} epochs · ${r.commit_count} commits · ${r.module_version_count} module-versions`;
      chip.onclick = () => {
        const i = State.activeRepos.indexOf(r.name);
        if (i >= 0) { if (State.activeRepos.length > 1) State.activeRepos.splice(i, 1); }
        else State.activeRepos.push(r.name);
        buildRepoToggles();
        Timeline.setRepos(State.activeRepos);
        Timeline.setPlayhead(State.ts);
        refresh(true);
      };
      box.appendChild(chip);
    });
  }

  function buildLegend(epochColors) {
    const L = document.getElementById('legend');
    let h = '<div style="color:var(--text-faint);margin-bottom:4px">projects</div>';
    State.repos.forEach(r => h += `<div class="row"><span class="swatch" style="background:${r.color}"></span>${r.canonical}</div>`);
    h += '<div style="color:var(--text-faint);margin:8px 0 4px">edges</div>';
    h += `<div class="row"><span class="edge-swatch" style="border-top:2px dashed #e0724a;width:22px"></span>evolved from (lineage)</div>`;
    h += `<div class="row"><span class="edge-swatch" style="border-top:2px dotted #94a3b8;width:22px"></span>similar concept</div>`;
    h += `<div class="row"><span class="edge-swatch" style="border-top:2px solid #4b5563;width:22px"></span>module dependency</div>`;
    h += '<div style="color:var(--text-faint);margin:8px 0 4px">epoch types</div>';
    Object.entries(epochColors).forEach(([k, c]) => h += `<div class="row"><span class="swatch" style="background:${c}"></span>${k}</div>`);
    L.innerHTML = h;
  }

  function renderStats(slice) {
    const s = slice.stats || {}; const per = s.per_repo || {};
    let h = '';
    State.activeRepos.forEach(r => {
      if (per[r]) h += `<div class="stat-row"><span class="dot" style="background:${State.repoColors[r]}"></span>${r}<span class="n">${per[r]}</span></div>`;
    });
    h += `<div class="stat-total">${s.nodes || 0} nodes · ${s.edges || 0} edges · <b>${slice.projection}</b></div>`;
    document.getElementById('sliceStats').innerHTML = h;
  }

  function renderEpochContext(ctx) {
    if (!ctx) return;
    const box = document.getElementById('epochContext');
    let h = '';
    State.activeRepos.forEach(r => {
      const e = ctx[r];
      if (e) h += `<div class="epoch-pill"><span class="bar" style="background:${(tl.epoch_colors || {})[e.epoch_type] || '#888'}"></span>
        <div><div><b>${r}</b> <span class="etype">${e.epoch_type}</span></div>
        <div style="color:var(--text-faint);font-size:10px">${e.git_tag || e.label || ''}</div></div></div>`;
    });
    box.innerHTML = h || '<span style="color:var(--text-faint)">no active epoch</span>';
  }

  function updateReadout() {
    document.getElementById('dateReadout').textContent =
      new Date(State.ts * 1000).toISOString().slice(0, 10);
  }
  function showLoading(on) { document.getElementById('loading').classList.toggle('hidden', !on); }

  // ── controls ──
  function bindControls() {
    document.getElementById('projectionSeg').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-proj]'); if (!b) return;
      State.projection = b.dataset.proj;
      [...e.currentTarget.children].forEach(c => c.classList.toggle('active', c === b));
      refresh(true);
    });
    document.getElementById('playBtn').onclick = () => State.playing ? stopPlay() : startPlay();
    document.getElementById('stepEpochFwd').onclick = () => { stopPlay(); Timeline.stepEpoch(1); };
    document.getElementById('stepEpochBack').onclick = () => { stopPlay(); Timeline.stepEpoch(-1); };
    document.getElementById('jumpStart').onclick = () => { stopPlay(); seek(State.tsMin); };
    document.getElementById('jumpEnd').onclick = () => { stopPlay(); seek(State.tsMax); };
    document.getElementById('speed').oninput = (e) => State.speed = +e.target.value;
    document.getElementById('fitBtn').onclick = () => Graph.fit();
    document.getElementById('relayoutBtn').onclick = () => Graph.relayout();
    document.getElementById('themeBtn').onclick = toggleTheme;

    const f = (id, key) => document.getElementById(id).addEventListener('change', (e) => {
      State.filters[key] = e.target.checked;
      Graph.setEdgeVisibility(State.filters);
      Graph.setProvenanceOnly(State.filters.provOnly);
    });
    f('showDepends', 'depends'); f('showCross', 'cross'); f('onlyProvenance', 'provOnly');

    // search
    const box = document.getElementById('searchBox');
    const dd = document.getElementById('searchResults');
    let stimer = null;
    box.addEventListener('input', () => {
      clearTimeout(stimer);
      stimer = setTimeout(async () => {
        const q = box.value.trim();
        if (!q) { dd.classList.add('hidden'); return; }
        const res = await API.search(q, State.activeRepos);
        dd.innerHTML = res.results.map(r =>
          `<div class="item" data-id="${r.id}"><span class="dot" style="width:8px;height:8px;border-radius:50%;background:${State.repoColors[r.repo] || '#888'}"></span>${r.label}<span class="r">${r.repo}</span></div>`).join('')
          || '<div class="item" style="color:var(--text-faint)">no matches</div>';
        dd.classList.remove('hidden');
        dd.querySelectorAll('.item[data-id]').forEach(it => it.onclick = () => {
          dd.classList.add('hidden'); box.value = '';
          const id = it.dataset.id;
          if (Graph.has(id)) { Graph.selectNode(id); Graph.centerOn(id); showProvenance(id); }
          else alert('That module is not present at the current time / projection. Move the time cursor or enable its project.');
        });
      }, 180);
    });
    document.addEventListener('click', (e) => { if (!e.target.closest('.search')) dd.classList.add('hidden'); });
  }

  function toggleTheme() {
    const root = document.documentElement;
    const dark = root.getAttribute('data-theme') !== 'light';
    root.setAttribute('data-theme', dark ? 'light' : 'dark');
    document.getElementById('themeBtn').textContent = dark ? '☀' : '☾';
  }
})();
