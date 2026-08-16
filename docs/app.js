(async function () {
  const state = {
    meta: null,
    overall: [],
    coins: {},
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

  function drawSparkline(containerId, data, key) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.innerHTML = '';
    if (!data.length) return;
    // Spec: mini sparkline covers the last 30 days (~720 hourly points).
    const recent = data.slice(-720);
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
    poly.setAttribute('stroke-width', '2');
    svg.appendChild(poly);
    container.appendChild(svg);
  }

  function updateCard(name, key) {
    const data = state.overall;
    if (!data.length) return;
    const cur = data[data.length - 1][key];
    const prev = data.length > 24 ? data[data.length - 25][key] : data[0][key];
    const change = cur - prev;
    document.getElementById(`value-${name}`).textContent = fmtNum(cur, 3);
    const changeEl = document.getElementById(`change-${name}`);
    changeEl.textContent = `${change >= 0 ? '+' : ''}${fmtNum(change, 3)} (24h)`;
    changeEl.className = `card-change ${signClass(change)}`;
    drawSparkline(`spark-${name}`, data, key);
  }

  function renderCards() {
    updateCard('overall-news', 'overall_news');
    updateCard('overall-social', 'overall_social');
    updateCard('market-news', 'market_news');
    updateCard('breadth', 'breadth');
  }

  function renderCoinSelector() {
    const select = document.getElementById('coin-select');
    select.innerHTML = '';
    const coins = state.meta.coins || [];
    coins.forEach(sym => {
      const opt = document.createElement('option');
      opt.value = sym;
      opt.textContent = sym;
      if (sym === state.selectedCoin) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener('change', e => {
      state.selectedCoin = e.target.value;
      loadCoinChart(state.selectedCoin);
    });
    document.getElementById('coin-search').addEventListener('input', e => {
      const q = e.target.value.toUpperCase();
      for (const opt of select.options) {
        opt.hidden = !opt.value.includes(q);
      }
    });
  }

  async function loadCoinChart(symbol) {
    const data = await loadJSON(`data/coins/${symbol}.json`);
    const news = data.filter(r => r.family === 'news');
    const social = data.filter(r => r.family === 'social');

    const container = document.getElementById('main-chart');
    container.innerHTML = '';
    const chart = LightweightCharts.createChart(container, {
      height: 420,
      layout: { background: { color: '#ffffff' }, textColor: '#1f2328' },
      grid: { vertLines: { color: '#eef0f2' }, horzLines: { color: '#eef0f2' } },
      rightPriceScale: { borderColor: '#d0d7de' },
      timeScale: { borderColor: '#d0d7de', timeVisible: true },
    });

    const newsLine = chart.addLineSeries({
      color: '#2e7d32',
      lineWidth: 2,
      title: 'News Sentiment',
    });
    const socialLine = chart.addLineSeries({
      color: '#c62828',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      title: 'Social Sentiment',
    });
    const mentionsSeries = chart.addHistogramSeries({
      color: '#57606a',
      priceFormat: { type: 'volume' },
      priceScaleId: 'mentions',
    });
    chart.priceScale('mentions').applyOptions({ scaleMargins: { top: 0.7, bottom: 0 } });

    function toCandle(r) {
      const time = new Date(r.ts).getTime() / 1000;
      return { time, value: r.sent };
    }

    // Spec: segments where the 30d z-score is unavailable (sent_z === null)
    // are drawn as dotted lines. Family style is preserved (news solid /
    // social dashed); z-null overrides to sparse dots on both.
    function splitByZ(records) {
      const solid = [], dotted = [];
      let prev = null;
      let prevDotted = false;
      for (const r of records) {
        const pt = toCandle(r);
        const isDotted = r.sent_z === null || r.sent_z === undefined;
        const target = isDotted ? dotted : solid;
        if (prev && isDotted !== prevDotted) target.push(prev); // continuity at the transition
        target.push(pt);
        prev = pt;
        prevDotted = isDotted;
      }
      return { solid, dotted };
    }

    const newsSplit = splitByZ(news);
    const socialSplit = splitByZ(social);
    newsLine.setData(newsSplit.solid);
    socialLine.setData(socialSplit.solid);

    const newsZNull = chart.addLineSeries({
      color: '#2e7d32',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.SparseDotted,
      title: 'News (z-score pending)',
    });
    const socialZNull = chart.addLineSeries({
      color: '#c62828',
      lineWidth: 2,
      lineStyle: LightweightCharts.LineStyle.SparseDotted,
      title: 'Social (z-score pending)',
    });
    newsZNull.setData(newsSplit.dotted);
    socialZNull.setData(socialSplit.dotted);

    const mentionsMap = new Map();
    [...news, ...social].forEach(r => {
      const t = new Date(r.ts).getTime() / 1000;
      mentionsMap.set(t, (mentionsMap.get(t) || 0) + (r.mentions || 0));
    });
    const mentionsData = Array.from(mentionsMap.entries())
      .map(([time, value]) => ({ time, value }))
      .sort((a, b) => a.time - b.time);
    mentionsSeries.setData(mentionsData);

    chart.timeScale().fitContent();
  }

  function computeLeaderboard() {
    // Spec: leaderboard ranks the *current 24h* average SENT, not the full history.
    const latest = state.overall.length
      ? new Date(state.overall[state.overall.length - 1].ts).getTime()
      : Date.now();
    const cutoff = latest - 24 * 3600 * 1000;
    const avg = {};
    Object.entries(state.coins).forEach(([symbol, records]) => {
      const recent = records.filter(r => new Date(r.ts).getTime() >= cutoff);
      const byFamily = { news: [], social: [] };
      recent.forEach(r => {
        if (byFamily[r.family]) byFamily[r.family].push(r.sent);
      });
      const newsAvg = byFamily.news.length ? byFamily.news.reduce((a, b) => a + b, 0) / byFamily.news.length : null;
      const socialAvg = byFamily.social.length ? byFamily.social.reduce((a, b) => a + b, 0) / byFamily.social.length : null;
      const all = [...byFamily.news, ...byFamily.social];
      const combined = all.length ? all.reduce((a, b) => a + b, 0) / all.length : null;
      const lowConfidence = recent.length > 0 && recent.every(r => r.confidence_flag === 'low');
      avg[symbol] = { news: newsAvg, social: socialAvg, combined, lowConfidence };
    });
    const ranked = Object.entries(avg)
      .filter(([_, v]) => v.combined !== null)
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
        if (v.lowConfidence) tr.classList.add('low-confidence');
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

  async function init() {
    state.meta = await loadJSON('data/meta.json');
    state.overall = await loadJSON('data/overall.json');
    const coinSymbols = state.meta.coins || [];
    await Promise.all(
      coinSymbols.map(async sym => {
        try {
          state.coins[sym] = await loadJSON(`data/coins/${sym}.json`);
        } catch (e) {
          state.coins[sym] = [];
        }
      })
    );

    document.getElementById('updated-at').textContent = `Updated: ${new Date(state.meta.updated_at).toLocaleString()}`;
    renderCards();
    renderCoinSelector();
    await loadCoinChart(state.selectedCoin);
    renderLeaderboard();
  }

  init().catch(err => {
    console.error(err);
    document.getElementById('updated-at').textContent = 'Error loading data. See console.';
  });
})();
