(async function () {
  const state = {
    meta: null,
    overall: [],
    coins: {},
    coinList: [], // normalized [{symbol, name}]
    selectedCoin: 'BTC',
  };

  async function loadJSON(url) {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`Failed to load ${url}: ${resp.status}`);
    return resp.json();
  }

  function fmtNum(n, digits = 3) {
    if (n === null || n === undefined || isNaN(n)) return '-';
    return Number(n).toFixed(digits);
  }

  function signClass(n) {
    if (n === null || n === undefined || isNaN(n)) return 'neutral';
    return n > 0 ? 'positive' : n < 0 ? 'negative' : 'neutral';
  }

  function badgeFor(n) {
    if (n === null || n === undefined || isNaN(n)) return { text: '-', cls: 'neutral' };
    if (n > 0.05) return { text: '偏多', cls: 'positive' };
    if (n < -0.05) return { text: '偏空', cls: 'negative' };
    return { text: '中性', cls: 'neutral' };
  }

  function tsOf(r) {
    return new Date(r.ts).getTime();
  }

  // Find the point closest to (but not after) the target timestamp.
  function pointAtOrBefore(data, targetMs) {
    let best = null;
    for (const d of data) {
      const t = tsOf(d);
      if (t <= targetMs && (best === null || t > tsOf(best))) best = d;
    }
    return best;
  }

  function drawSparkline(containerId, data, key) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = '';
    if (!data.length) return;
    // Mini trend covers the last 30 days.
    const cutoff = tsOf(data[data.length - 1]) - 30 * 24 * 3600 * 1000;
    const recent = data.filter(d => tsOf(d) >= cutoff);
    const vals = recent.map(d => d[key]).filter(v => v !== null && !isNaN(v));
    if (vals.length < 2) return;
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const range = max - min || 1;
    const width = container.clientWidth;
    const height = container.clientHeight;
    const step = width / (vals.length - 1);
    let path = '';
    vals.forEach((v, i) => {
      const x = i * step;
      const y = height - ((v - min) / range) * (height - 4) - 2;
      path += (i === 0 ? 'M' : 'L') + `${x},${y}`;
    });
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', width);
    svg.setAttribute('height', height);
    const poly = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    const last = vals[vals.length - 1];
    const color = last >= 0 ? '#2e7d32' : '#c62828';
    poly.setAttribute('d', path);
    poly.setAttribute('fill', 'none');
    poly.setAttribute('stroke', color);
    poly.setAttribute('stroke-width', '1.5');
    svg.appendChild(poly);
    container.appendChild(svg);
  }

  function updateCard(name, key) {
    const data = state.overall;
    if (!data.length) return;
    const latest = data[data.length - 1];
    const cur = latest[key];
    // 24h change by timestamp, robust to 15-min slots and gaps.
    const prevPoint = pointAtOrBefore(data, tsOf(latest) - 24 * 3600 * 1000) || data[0];
    const change = cur - prevPoint[key];
    const valueEl = document.getElementById(`value-${name}`);
    valueEl.textContent = fmtNum(cur, 3);
    valueEl.className = `card-value ${signClass(cur)}`;
    const badge = badgeFor(cur);
    const badgeEl = document.getElementById(`badge-${name}`);
    badgeEl.textContent = badge.text;
    badgeEl.className = `badge ${badge.cls}`;
    const changeEl = document.getElementById(`change-${name}`);
    changeEl.textContent = `24h ${change >= 0 ? '+' : ''}${fmtNum(change, 3)}`;
    changeEl.className = `card-change ${signClass(change)}`;
    drawSparkline(`spark-${name}`, data, key);
  }

  function renderCards() {
    updateCard('overall-news', 'overall_news');
    updateCard('overall-social', 'overall_social');
    updateCard('market-news', 'market_news');
    updateCard('breadth', 'breadth');
  }

  function normalizeCoinList(meta) {
    // Compatible with both the old format (["BTC", ...]) and the new
    // format ([{symbol, name}, ...]).
    return (meta.coins || []).map(c =>
      typeof c === 'string' ? { symbol: c, name: c } : c
    );
  }

  function renderCoinSelector() {
    const select = document.getElementById('coin-select');
    select.innerHTML = '';
    state.coinList.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.symbol;
      opt.textContent = c.name && c.name !== c.symbol ? `${c.symbol} · ${c.name}` : c.symbol;
      if (c.symbol === state.selectedCoin) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener('change', e => {
      state.selectedCoin = e.target.value;
      loadCoinCharts(state.selectedCoin);
    });
    document.getElementById('coin-search').addEventListener('input', e => {
      const q = e.target.value.toUpperCase();
      for (const opt of select.options) {
        opt.hidden = !opt.textContent.toUpperCase().includes(q);
      }
    });
  }

  function toPoint(r) {
    return { time: Math.floor(tsOf(r) / 1000), value: r.sent };
  }

  // Split records into segments by rendering class. Priority:
  // carried (gray dotted) > z-null (family-colored sparse dots) > normal.
  function splitSegments(records) {
    const normal = [], znull = [], carried = [];
    let prev = null;
    let prevCls = null;
    for (const r of records) {
      const cls = r.confidence_flag === 'carried'
        ? 'carried'
        : (r.sent_z === null || r.sent_z === undefined) ? 'znull' : 'normal';
      const pt = toPoint(r);
      const target = cls === 'carried' ? carried : cls === 'znull' ? znull : normal;
      if (prev && cls !== prevCls) target.push(prev); // continuity at the transition
      target.push(pt);
      prev = pt;
      prevCls = cls;
    }
    return { normal, znull, carried };
  }

  // Build one family chart (news or social) into the given container.
  function buildFamilyChart(containerId, records, color, lineStyle, title) {
    const container = document.getElementById(containerId);
    container.innerHTML = '';
    const chart = LightweightCharts.createChart(container, {
      height: 320,
      layout: { background: { color: '#ffffff' }, textColor: '#1f2328' },
      grid: { vertLines: { color: '#eef0f2' }, horzLines: { color: '#eef0f2' } },
      rightPriceScale: { borderColor: '#d0d7de' },
      timeScale: { borderColor: '#d0d7de', timeVisible: true, secondsVisible: false },
    });

    const split = splitSegments(records);

    const mainLine = chart.addLineSeries({ color, lineWidth: 2, lineStyle, title });
    mainLine.setData(split.normal);

    const zNullLine = chart.addLineSeries({
      color, lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.SparseDotted,
      title: `${title}（z 未启用）`,
    });
    zNullLine.setData(split.znull);

    const carriedLine = chart.addLineSeries({
      color: '#9fa5ab', lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.SparseDotted,
      title: '沿用前值（无新数据）',
    });
    carriedLine.setData(split.carried);

    const mentionsSeries = chart.addHistogramSeries({
      color: '#b9bfc7',
      priceFormat: { type: 'volume' },
      priceScaleId: 'mentions',
    });
    chart.priceScale('mentions').applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
    mentionsSeries.setData(
      records
        .map(r => ({ time: Math.floor(tsOf(r) / 1000), value: r.mentions || 0 }))
        .sort((a, b) => a.time - b.time)
    );

    chart.timeScale().fitContent();
    return chart;
  }

  async function loadCoinCharts(symbol) {
    const data = await loadJSON(`data/coins/${symbol}.json`);
    const news = data.filter(r => r.family === 'news');
    const social = data.filter(r => r.family === 'social');

    // Status line: latest values and whether they are carried forward.
    const statusEl = document.getElementById('coin-status');
    const latestNews = news.length ? news[news.length - 1] : null;
    const latestSocial = social.length ? social[social.length - 1] : null;
    function statusPart(label, rec) {
      if (!rec) return `${label} 暂无数据`;
      const carried = rec.confidence_flag === 'carried' ? '（沿用前值）' : '';
      return `${label} ${fmtNum(rec.sent)}${carried}`;
    }
    statusEl.textContent = `当前：${statusPart('新闻', latestNews)} / ${statusPart('社媒', latestSocial)}`;

    buildFamilyChart(
      'chart-news', news, '#2e7d32',
      LightweightCharts.LineStyle.Solid, '新闻情绪'
    );
    buildFamilyChart(
      'chart-social', social, '#c62828',
      LightweightCharts.LineStyle.Solid, '社媒情绪'
    );
  }

  function computeLeaderboard() {
    // Ranks the current 24h average SENT, not the full history.
    const latest = state.overall.length
      ? tsOf(state.overall[state.overall.length - 1])
      : Date.now();
    const cutoff = latest - 24 * 3600 * 1000;
    const avg = {};
    Object.entries(state.coins).forEach(([symbol, records]) => {
      const recent = records.filter(r => tsOf(r) >= cutoff);
      if (!recent.length) return;
      // Fresh points carry real information; carried points are stale echoes.
      const fresh = recent.filter(r => r.confidence_flag !== 'carried');
      const used = fresh.length ? fresh : recent;
      const byFamily = { news: [], social: [] };
      used.forEach(r => {
        if (byFamily[r.family]) byFamily[r.family].push(r.sent);
      });
      const newsAvg = byFamily.news.length ? byFamily.news.reduce((a, b) => a + b, 0) / byFamily.news.length : null;
      const socialAvg = byFamily.social.length ? byFamily.social.reduce((a, b) => a + b, 0) / byFamily.social.length : null;
      const all = [...byFamily.news, ...byFamily.social];
      const combined = all.length ? all.reduce((a, b) => a + b, 0) / all.length : null;
      const stale = fresh.length === 0 || fresh.every(r => r.confidence_flag === 'low');
      avg[symbol] = { news: newsAvg, social: socialAvg, combined, stale };
    });
    const ranked = Object.entries(avg)
      .filter(([, v]) => v.combined !== null)
      .sort((a, b) => b[1].combined - a[1].combined);
    return { ranked, avg };
  }

  function renderLeaderboard() {
    const { ranked } = computeLeaderboard();
    const top = ranked.slice(0, 10);
    const bottom = ranked.slice(-10).reverse();

    function fill(tableId, rows) {
      const tbody = document.querySelector(`#${tableId} tbody`);
      tbody.innerHTML = '';
      rows.forEach(([sym, v]) => {
        const tr = document.createElement('tr');
        if (v.stale) tr.classList.add('low-confidence');
        tr.innerHTML = `
          <td>${sym}</td>
          <td class="${signClass(v.news)}">${fmtNum(v.news)}</td>
          <td class="${signClass(v.social)}">${fmtNum(v.social)}</td>
        `;
        tbody.appendChild(tr);
      });
    }
    fill('top-table', top);
    fill('bottom-table', bottom);
  }

  function renderHeaderMeta() {
    const updated = new Date(state.meta.updated_at);
    document.getElementById('updated-at').textContent =
      `数据更新于 ${updated.toLocaleString('zh-CN')}`;
    const q = state.meta.quality_summary || {};
    const qEl = document.getElementById('quality-info');
    if (q.agreement_rate !== null && q.agreement_rate !== undefined) {
      qEl.textContent = `最近自校验一致率 ${(q.agreement_rate * 100).toFixed(0)}%（样本 ${q.sample_n}）`;
    } else {
      qEl.textContent = '自校验暂未运行';
    }
  }

  async function init() {
    state.meta = await loadJSON('data/meta.json');
    state.overall = await loadJSON('data/overall.json');
    state.coinList = normalizeCoinList(state.meta);
    await Promise.all(
      state.coinList.map(async c => {
        try {
          state.coins[c.symbol] = await loadJSON(`data/coins/${c.symbol}.json`);
        } catch (e) {
          state.coins[c.symbol] = [];
        }
      })
    );
    if (!state.coinList.some(c => c.symbol === state.selectedCoin) && state.coinList.length) {
      state.selectedCoin = state.coinList[0].symbol;
    }

    renderHeaderMeta();
    renderCards();
    renderCoinSelector();
    await loadCoinCharts(state.selectedCoin);
    renderLeaderboard();
  }

  init().catch(err => {
    console.error(err);
    document.getElementById('updated-at').textContent = '数据加载失败，请查看控制台。';
  });
})();
