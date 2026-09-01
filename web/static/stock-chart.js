(() => {
  'use strict';

  const API_PREFIX = '/api/chart/';
  const LW_URL = 'https://unpkg.com/lightweight-charts@4.2.2/dist/lightweight-charts.standalone.production.js';
  let chartLibPromise = null;
  let activeSymbol = null;
  let activeTimeframe = '15m';
  let requestToken = 0;
  let activeChart = null;
  let resizeObserver = null;

  const originalOpenStockModal = window.openStockModal;
  const originalCloseStockModal = window.closeStockModal;

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function fmtPrice(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '-';
    if (Math.abs(n) >= 100) return '$' + n.toFixed(2);
    if (Math.abs(n) >= 1) return '$' + n.toFixed(3);
    return '$' + n.toFixed(4);
  }

  function fmtPct(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '-';
    return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
  }

  function label(value) {
    const labels = {
      STRONG_BULLISH: 'Güçlü Yükseliş',
      BULLISH: 'Yükseliş',
      BEARISH: 'Düşüş',
      TRANSITION: 'Geçiş / Range',
      HH_HL: 'HH + HL',
      LH_LL: 'LH + LL',
      MIXED: 'Karışık',
      BREAKOUT_CONFIRMED: 'Breakout Teyitli',
      BREAKOUT_READY: 'Breakout Hazır',
      SUPPORT_BOUNCE: 'Destekten Tepki',
      TREND_PULLBACK: 'Trend Pullback',
      RANGE_OR_TRANSITION: 'Range / Geçiş',
      NO_CLEAN_SETUP: 'Temiz Setup Yok',
      STRONG_CANDIDATE: 'Güçlü Aday',
      CANDIDATE: 'Aday',
      WATCH: 'Takip',
      AVOID: 'Kaçın'
    };
    return labels[value] || value || '-';
  }

  function tagClass(decision) {
    if (decision === 'STRONG_CANDIDATE' || decision === 'CANDIDATE') return 'tag-buy';
    if (decision === 'AVOID') return 'tag-sell';
    return 'tag-watch';
  }

  function ensureStyles() {
    if (document.getElementById('nasdq-chart-analysis-style')) return;
    const style = document.createElement('style');
    style.id = 'nasdq-chart-analysis-style';
    style.textContent = `
      .nasdq-chart-toolbar{display:flex;align-items:center;justify-content:space-between;gap:.65rem;flex-wrap:wrap;margin-bottom:.65rem}
      .nasdq-tf-group{display:flex;gap:.35rem}
      .nasdq-tf-btn{border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.04);color:#94a3b8;border-radius:7px;padding:.36rem .68rem;font-size:.72rem;font-weight:700;cursor:pointer;transition:.15s ease}
      .nasdq-tf-btn:hover{border-color:rgba(59,130,246,.45);color:#f8fafc}
      .nasdq-tf-btn.active{background:rgba(59,130,246,.18);border-color:rgba(59,130,246,.55);color:#60a5fa}
      .nasdq-chart-method{font-size:.68rem;color:#64748b}
      .nasdq-chart-canvas{width:100%;height:390px;border:1px solid rgba(255,255,255,.06);border-radius:8px;overflow:hidden;background:#0b1220}
      .nasdq-chart-loading{height:390px;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:.55rem;color:#94a3b8;font-size:.78rem}
      .nasdq-spinner{width:26px;height:26px;border:3px solid rgba(255,255,255,.12);border-top-color:#3b82f6;border-radius:50%;animation:nasdqSpin .8s linear infinite}
      @keyframes nasdqSpin{to{transform:rotate(360deg)}}
      .nasdq-chart-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:.5rem;margin-top:.7rem}
      .nasdq-metric{background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:.55rem .62rem}
      .nasdq-metric-k{font-size:.62rem;letter-spacing:.04em;text-transform:uppercase;color:#64748b;font-weight:700}
      .nasdq-metric-v{margin-top:.18rem;font-family:'JetBrains Mono',monospace;color:#f8fafc;font-size:.82rem;font-weight:700}
      .nasdq-chart-error{padding:1rem;border:1px solid rgba(239,68,68,.28);background:rgba(239,68,68,.08);border-radius:8px;color:#fca5a5;font-size:.78rem}
      .nasdq-legend{display:flex;gap:.55rem;flex-wrap:wrap;font-size:.63rem;color:#94a3b8;margin-top:.45rem}
      .nasdq-legend span{display:inline-flex;align-items:center;gap:.25rem}
      .nasdq-dot{width:7px;height:7px;border-radius:50%;display:inline-block}
      @media(max-width:720px){.nasdq-chart-canvas,.nasdq-chart-loading{height:320px}.nasdq-chart-method{display:none}}
    `;
    document.head.appendChild(style);
  }

  function ensureChartLib() {
    if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
    if (chartLibPromise) return chartLibPromise;
    chartLibPromise = new Promise((resolve, reject) => {
      const existing = document.querySelector('script[data-nasdq-lightweight-charts]');
      if (existing) {
        existing.addEventListener('load', () => resolve(window.LightweightCharts));
        existing.addEventListener('error', reject);
        return;
      }
      const script = document.createElement('script');
      script.src = LW_URL;
      script.async = true;
      script.dataset.nasdqLightweightCharts = '1';
      script.onload = () => resolve(window.LightweightCharts);
      script.onerror = () => reject(new Error('Grafik kütüphanesi yüklenemedi'));
      document.head.appendChild(script);
    });
    return chartLibPromise;
  }

  function destroyChart() {
    if (resizeObserver) {
      resizeObserver.disconnect();
      resizeObserver = null;
    }
    if (activeChart) {
      try { activeChart.remove(); } catch (_) {}
      activeChart = null;
    }
  }

  function renderShell(symbol, timeframe) {
    const container = document.getElementById('modalTvContainer');
    if (!container) return;
    destroyChart();
    container.innerHTML = `
      <div class="nasdq-chart-toolbar">
        <div class="nasdq-tf-group">
          ${['15m','1h','1d'].map(tf => `<button class="nasdq-tf-btn ${tf === timeframe ? 'active' : ''}" data-chart-tf="${tf}">${tf === '1d' ? '1D' : tf === '1h' ? '1H' : '15M'}</button>`).join('')}
        </div>
        <div class="nasdq-chart-method">Deterministic Chart Reader · trend → structure → location → volume</div>
      </div>
      <div id="nasdqChartCanvas" class="nasdq-chart-canvas">
        <div class="nasdq-chart-loading"><div class="nasdq-spinner"></div><div>${escapeHtml(symbol)} ${escapeHtml(timeframe)} grafik analizi hazırlanıyor...</div></div>
      </div>
      <div id="nasdqChartMetrics"></div>
    `;
    container.querySelectorAll('[data-chart-tf]').forEach(btn => {
      btn.addEventListener('click', () => {
        const tf = btn.getAttribute('data-chart-tf');
        if (!tf || tf === activeTimeframe) return;
        activeTimeframe = tf;
        loadAnalysis(activeSymbol, tf);
      });
    });
  }

  function lineSeries(chart, data, color, width = 1, style = 0) {
    if (!Array.isArray(data) || data.length === 0) return null;
    const series = chart.addLineSeries({
      color,
      lineWidth: width,
      lineStyle: style,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    series.setData(data);
    return series;
  }

  async function drawChart(payload) {
    const L = await ensureChartLib();
    const canvas = document.getElementById('nasdqChartCanvas');
    if (!canvas || payload.symbol !== activeSymbol || payload.timeframe !== activeTimeframe) return;
    destroyChart();
    canvas.innerHTML = '';

    const chart = L.createChart(canvas, {
      width: Math.max(canvas.clientWidth, 300),
      height: Math.max(canvas.clientHeight, 300),
      layout: { background: { type: 'solid', color: '#0b1220' }, textColor: '#94a3b8' },
      grid: { vertLines: { color: 'rgba(148,163,184,.07)' }, horzLines: { color: 'rgba(148,163,184,.07)' } },
      crosshair: { mode: L.CrosshairMode.Normal },
      rightPriceScale: { borderColor: 'rgba(148,163,184,.18)' },
      timeScale: { borderColor: 'rgba(148,163,184,.18)', timeVisible: payload.timeframe !== '1d', secondsVisible: false },
      handleScroll: true,
      handleScale: true,
    });
    activeChart = chart;

    const candles = chart.addCandlestickSeries({
      upColor: '#10b981', downColor: '#ef4444', borderVisible: false,
      wickUpColor: '#10b981', wickDownColor: '#ef4444',
    });
    candles.setData(payload.candles || []);

    const volume = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: '',
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    volume.setData((payload.volume || []).map(v => ({
      time: v.time,
      value: v.value,
      color: v.direction === 'up' ? 'rgba(16,185,129,.32)' : 'rgba(239,68,68,.32)'
    })));

    const ind = payload.indicators || {};
    lineSeries(chart, ind.ema9, '#f59e0b', 1);
    lineSeries(chart, ind.ema21, '#3b82f6', 2);
    lineSeries(chart, ind.ema50, '#8b5cf6', 2);
    lineSeries(chart, ind.vwap, '#22d3ee', 1, 2);
    lineSeries(chart, ind.bbUpper, 'rgba(148,163,184,.42)', 1, 2);
    lineSeries(chart, ind.bbLower, 'rgba(148,163,184,.42)', 1, 2);

    const levels = payload.levels || {};
    const plan = payload.trade_plan || {};
    if (Number.isFinite(Number(levels.support))) {
      candles.createPriceLine({ price: Number(levels.support), color: '#10b981', lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: 'DESTEK' });
    }
    if (Number.isFinite(Number(levels.resistance))) {
      candles.createPriceLine({ price: Number(levels.resistance), color: '#ef4444', lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: 'DİRENÇ' });
    }
    if (Number.isFinite(Number(plan.entry))) {
      candles.createPriceLine({ price: Number(plan.entry), color: '#e2e8f0', lineWidth: 1, lineStyle: 3, axisLabelVisible: false, title: 'ENTRY' });
    }
    if (Number.isFinite(Number(plan.stop))) {
      candles.createPriceLine({ price: Number(plan.stop), color: '#fb7185', lineWidth: 1, lineStyle: 3, axisLabelVisible: true, title: 'SL' });
    }
    if (Number.isFinite(Number(plan.target1))) {
      candles.createPriceLine({ price: Number(plan.target1), color: '#34d399', lineWidth: 1, lineStyle: 3, axisLabelVisible: true, title: 'TP1' });
    }

    chart.timeScale().fitContent();
    resizeObserver = new ResizeObserver(entries => {
      const entry = entries[0];
      if (!entry || !activeChart) return;
      activeChart.applyOptions({ width: Math.floor(entry.contentRect.width) });
    });
    resizeObserver.observe(canvas);

    const legend = document.createElement('div');
    legend.className = 'nasdq-legend';
    legend.innerHTML = `
      <span><i class="nasdq-dot" style="background:#f59e0b"></i>EMA9</span>
      <span><i class="nasdq-dot" style="background:#3b82f6"></i>EMA21</span>
      <span><i class="nasdq-dot" style="background:#8b5cf6"></i>EMA50</span>
      <span><i class="nasdq-dot" style="background:#22d3ee"></i>VWAP</span>
      <span><i class="nasdq-dot" style="background:#10b981"></i>Destek</span>
      <span><i class="nasdq-dot" style="background:#ef4444"></i>Direnç</span>`;
    canvas.parentElement.appendChild(legend);
  }

  function updateAnalysisPanels(payload) {
    const s = payload.snapshot || {};
    const plan = payload.trade_plan || {};

    const title = document.getElementById('modalTitle');
    if (title) title.textContent = `${payload.symbol} — Grafik Analizi`;
    const price = document.getElementById('modalPrice');
    if (price) price.textContent = fmtPrice(s.price);
    const change = document.getElementById('modalChange');
    if (change) {
      change.textContent = fmtPct(s.change_pct);
      change.className = `m-val mono ${Number(s.change_pct) >= 0 ? 'price-up' : 'price-down'}`;
    }
    const setupScore = document.getElementById('modalSetupScore');
    if (setupScore) {
      setupScore.innerHTML = `<span class="tag ${tagClass(s.decision)}">${escapeHtml(label(s.decision))}</span> <strong style="font-size:1.1rem;color:var(--green)">${Number(s.score || 0).toFixed(0)} / 100</strong>`;
    }
    const horizon = document.getElementById('modalHorizon');
    if (horizon) horizon.textContent = payload.timeframe === '15m' ? '15 Dakika' : payload.timeframe === '1h' ? '1 Saat' : '1 Gün';

    const metrics = document.getElementById('nasdqChartMetrics');
    if (metrics) {
      metrics.innerHTML = `
        <div class="nasdq-chart-grid">
          <div class="nasdq-metric"><div class="nasdq-metric-k">Trend</div><div class="nasdq-metric-v">${escapeHtml(label(s.trend))}</div></div>
          <div class="nasdq-metric"><div class="nasdq-metric-k">Market Structure</div><div class="nasdq-metric-v">${escapeHtml(label(s.market_structure))}</div></div>
          <div class="nasdq-metric"><div class="nasdq-metric-k">Setup</div><div class="nasdq-metric-v">${escapeHtml(label(s.setup))}</div></div>
          <div class="nasdq-metric"><div class="nasdq-metric-k">RSI 14</div><div class="nasdq-metric-v">${Number(s.rsi14 || 0).toFixed(1)}</div></div>
          <div class="nasdq-metric"><div class="nasdq-metric-k">RVOL</div><div class="nasdq-metric-v">${Number(s.rvol || 0).toFixed(2)}x</div></div>
          <div class="nasdq-metric"><div class="nasdq-metric-k">ATR 14</div><div class="nasdq-metric-v">${fmtPrice(s.atr14)}</div></div>
          <div class="nasdq-metric"><div class="nasdq-metric-k">Destek</div><div class="nasdq-metric-v" style="color:#34d399">${fmtPrice(s.support)}</div></div>
          <div class="nasdq-metric"><div class="nasdq-metric-k">Direnç</div><div class="nasdq-metric-v" style="color:#fb7185">${fmtPrice(s.resistance)}</div></div>
        </div>`;
    }

    const reasons = document.getElementById('modalReasons');
    if (reasons && Array.isArray(payload.explanation)) {
      reasons.innerHTML = payload.explanation.map(text => `
        <div class="reason-item"><span class="reason-bullet">⚡</span><div>${escapeHtml(text)}</div></div>`).join('');
    }

    const type = document.getElementById('modalAnalysisType');
    if (type) {
      type.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:.55rem">
          <div style="background:rgba(59,130,246,.06);border:1px solid rgba(59,130,246,.2);padding:.7rem;border-radius:8px"><div style="font-size:.68rem;color:var(--muted);font-weight:700">TREND</div><div style="font-size:.9rem;font-weight:700;margin-top:.2rem">${escapeHtml(label(s.trend))}</div></div>
          <div style="background:rgba(139,92,246,.06);border:1px solid rgba(139,92,246,.2);padding:.7rem;border-radius:8px"><div style="font-size:.68rem;color:var(--muted);font-weight:700">YAPI</div><div style="font-size:.9rem;font-weight:700;margin-top:.2rem">${escapeHtml(label(s.market_structure))}</div></div>
          <div style="background:rgba(16,185,129,.06);border:1px solid rgba(16,185,129,.2);padding:.7rem;border-radius:8px"><div style="font-size:.68rem;color:var(--muted);font-weight:700">SETUP</div><div style="font-size:.9rem;font-weight:700;margin-top:.2rem">${escapeHtml(label(s.setup))}</div></div>
        </div>
        <div style="margin-top:.65rem;font-size:.72rem;color:var(--muted);line-height:1.45">Bu okuma sonuç/gelecek fiyat verisi kullanmadan; tamamlanmış mumlar, onaylanmış swing yapısı, EMA/VWAP konumu, destek-dirence mesafe ve hacim teyidiyle üretilir.</div>`;
    }

    const tradePlan = document.getElementById('modalTradePlan');
    if (tradePlan) {
      tradePlan.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(105px,1fr));gap:.5rem;text-align:center">
          <div style="padding:.5rem;background:rgba(255,255,255,.03);border-radius:6px"><div style="font-size:.65rem;color:var(--muted)">ENTRY</div><div class="mono" style="font-size:.86rem;font-weight:700">${fmtPrice(plan.entry)}</div></div>
          <div style="padding:.5rem;background:rgba(239,68,68,.08);border-radius:6px"><div style="font-size:.65rem;color:var(--red)">STOP</div><div class="mono price-down" style="font-size:.86rem;font-weight:700">${fmtPrice(plan.stop)}</div></div>
          <div style="padding:.5rem;background:rgba(16,185,129,.08);border-radius:6px"><div style="font-size:.65rem;color:var(--green)">TP1 · 2R</div><div class="mono price-up" style="font-size:.86rem;font-weight:700">${fmtPrice(plan.target1)}</div></div>
          <div style="padding:.5rem;background:rgba(16,185,129,.05);border-radius:6px"><div style="font-size:.65rem;color:var(--green)">TP2 · 3R</div><div class="mono price-up" style="font-size:.86rem;font-weight:700">${fmtPrice(plan.target2)}</div></div>
        </div>
        <div style="margin-top:.55rem;font-size:.7rem;color:var(--muted)">Risk/share: <strong>${fmtPrice(plan.risk_per_share)}</strong> · R:R TP1 ${Number(plan.rr1 || 0).toFixed(1)} · TP2 ${Number(plan.rr2 || 0).toFixed(1)} · Seviyeler ATR + grafik yapısı ile hesaplanır.</div>`;
    }
  }

  async function loadAnalysis(symbol, timeframe) {
    if (!symbol) return;
    activeSymbol = symbol;
    activeTimeframe = timeframe || '15m';
    const myToken = ++requestToken;
    renderShell(symbol, activeTimeframe);

    try {
      const response = await fetch(`${API_PREFIX}${encodeURIComponent(symbol)}?timeframe=${encodeURIComponent(activeTimeframe)}`, { cache: 'no-store' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
      if (myToken !== requestToken) return;
      updateAnalysisPanels(payload);
      await drawChart(payload);
    } catch (error) {
      if (myToken !== requestToken) return;
      const canvas = document.getElementById('nasdqChartCanvas');
      if (canvas) {
        canvas.innerHTML = `<div class="nasdq-chart-loading"><div class="nasdq-chart-error">Grafik analizi yüklenemedi: ${escapeHtml(error && error.message ? error.message : error)}</div><a class="tv-link" target="_blank" rel="noopener" href="https://www.tradingview.com/chart/?symbol=${encodeURIComponent(symbol)}">TradingView'da aç ↗</a></div>`;
      }
      console.error('Chart analysis failed', error);
    }
  }

  ensureStyles();

  window.openStockModal = function(symbol) {
    if (!symbol || symbol === 'SİSTEM') return;
    if (typeof originalOpenStockModal === 'function') {
      originalOpenStockModal(symbol);
    } else {
      const modal = document.getElementById('stockModal');
      if (modal) modal.classList.add('active');
    }
    activeSymbol = symbol;
    activeTimeframe = '15m';
    loadAnalysis(symbol, activeTimeframe);
  };

  window.closeStockModal = function(event) {
    requestToken++;
    destroyChart();
    activeSymbol = null;
    if (typeof originalCloseStockModal === 'function') return originalCloseStockModal(event);
    const modal = document.getElementById('stockModal');
    if (modal) modal.classList.remove('active');
  };
})();
