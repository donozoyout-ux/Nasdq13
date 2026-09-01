(() => {
  'use strict';

  const MTF_API = '/api/mtf-chart/';
  let activeRequest = 0;
  const cache = new Map();

  const previousOpenStockModal = window.openStockModal;
  const previousCloseStockModal = window.closeStockModal;

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function label(value) {
    const map = {
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
      WAIT_FOR_TRIGGER: '15M Tetik Bekle',
      AVOID: 'Kaçın',
      LONG_BIAS: 'Long Bias',
      RISK_OFF: 'Risk Off',
      NEUTRAL: 'Nötr',
      READY: 'Hazır',
      FORMING: 'Oluşuyor',
      WAIT: 'Bekle',
      INVALID: 'Geçersiz',
      TRIGGERED: 'Tetiklendi',
      WEAK: 'Zayıf',
      FULL: 'Tam Uyum',
      PARTIAL: 'Kısmi Uyum',
      CONFLICT: 'Çelişki'
    };
    return map[value] || value || '-';
  }

  function decisionClass(value) {
    if (value === 'STRONG_CANDIDATE' || value === 'CANDIDATE') return 'mtf-positive';
    if (value === 'AVOID' || value === 'CONFLICT' || value === 'RISK_OFF') return 'mtf-negative';
    return 'mtf-neutral';
  }

  function ensureStyles() {
    if (document.getElementById('nasdq-mtf-style')) return;
    const style = document.createElement('style');
    style.id = 'nasdq-mtf-style';
    style.textContent = `
      .nasdq-mtf-box{margin:1rem 0 0;padding:1rem;border-radius:12px;border:1px solid rgba(59,130,246,.22);background:linear-gradient(135deg,rgba(59,130,246,.075),rgba(139,92,246,.045));}
      .nasdq-mtf-head{display:flex;align-items:center;justify-content:space-between;gap:.65rem;flex-wrap:wrap;margin-bottom:.75rem}
      .nasdq-mtf-title{font-size:.82rem;font-weight:800;color:#f8fafc;display:flex;gap:.4rem;align-items:center}
      .nasdq-mtf-sub{font-size:.65rem;color:#64748b;margin-top:.12rem}
      .nasdq-mtf-score{display:flex;align-items:center;gap:.45rem;font-size:.72rem}
      .nasdq-mtf-pill{padding:.28rem .5rem;border-radius:999px;border:1px solid rgba(255,255,255,.1);font-weight:800}
      .mtf-positive{color:#34d399;background:rgba(16,185,129,.11);border-color:rgba(16,185,129,.28)}
      .mtf-negative{color:#fb7185;background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.28)}
      .mtf-neutral{color:#fbbf24;background:rgba(245,158,11,.08);border-color:rgba(245,158,11,.24)}
      .nasdq-mtf-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.55rem}
      .nasdq-mtf-tf{padding:.68rem;border-radius:9px;background:rgba(8,12,20,.52);border:1px solid rgba(255,255,255,.065)}
      .nasdq-mtf-tf-head{display:flex;align-items:center;justify-content:space-between;gap:.4rem;margin-bottom:.45rem}
      .nasdq-mtf-tf-name{font-family:'JetBrains Mono',monospace;font-size:.78rem;font-weight:900;color:#60a5fa}
      .nasdq-mtf-tf-score{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:#cbd5e1}
      .nasdq-mtf-row{display:flex;justify-content:space-between;gap:.5rem;padding:.18rem 0;font-size:.67rem;color:#94a3b8}
      .nasdq-mtf-row strong{font-weight:700;color:#e2e8f0;text-align:right}
      .nasdq-mtf-flow{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.45rem;margin-top:.65rem}
      .nasdq-mtf-flow-item{padding:.48rem .55rem;border-radius:8px;background:rgba(255,255,255,.03);font-size:.65rem;color:#94a3b8}
      .nasdq-mtf-flow-item b{display:block;color:#f8fafc;font-size:.69rem;margin-top:.12rem}
      .nasdq-mtf-reasons{margin-top:.7rem;display:grid;gap:.3rem}
      .nasdq-mtf-reason{font-size:.67rem;line-height:1.4;color:#94a3b8;padding-left:.9rem;position:relative}
      .nasdq-mtf-reason:before{content:'›';position:absolute;left:.15rem;color:#60a5fa;font-weight:900}
      .nasdq-mtf-loading{padding:.8rem;text-align:center;color:#94a3b8;font-size:.72rem}
      .nasdq-mtf-error{padding:.7rem;border-radius:8px;color:#fca5a5;background:rgba(239,68,68,.07);font-size:.7rem}
      @media(max-width:760px){.nasdq-mtf-grid,.nasdq-mtf-flow{grid-template-columns:1fr}}
    `;
    document.head.appendChild(style);
  }

  function ensurePanel() {
    ensureStyles();
    const modal = document.getElementById('stockModal');
    if (!modal) return null;
    let panel = document.getElementById('nasdqMtfPanel');
    if (panel) return panel;
    const card = modal.querySelector('.modal-card');
    if (!card) return null;
    panel = document.createElement('section');
    panel.id = 'nasdqMtfPanel';
    panel.className = 'nasdq-mtf-box';
    panel.innerHTML = '<div class="nasdq-mtf-loading">1D → 1H → 15M top-down analiz hazırlanıyor...</div>';
    card.appendChild(panel);
    return panel;
  }

  function tfCard(tf, data) {
    const name = tf === '1d' ? '1D · REJİM' : tf === '1h' ? '1H · SETUP' : '15M · TETİK';
    return `
      <div class="nasdq-mtf-tf">
        <div class="nasdq-mtf-tf-head"><span class="nasdq-mtf-tf-name">${name}</span><span class="nasdq-mtf-tf-score">${Number(data.score || 0).toFixed(0)}/100</span></div>
        <div class="nasdq-mtf-row"><span>Trend</span><strong>${esc(label(data.trend))}</strong></div>
        <div class="nasdq-mtf-row"><span>Yapı</span><strong>${esc(label(data.market_structure))}</strong></div>
        <div class="nasdq-mtf-row"><span>Setup</span><strong>${esc(label(data.setup))}</strong></div>
        <div class="nasdq-mtf-row"><span>RSI / RVOL</span><strong>${Number(data.rsi14 || 0).toFixed(1)} / ${Number(data.rvol || 0).toFixed(2)}x</strong></div>
      </div>`;
  }

  function render(payload) {
    const panel = ensurePanel();
    if (!panel) return;
    const s = payload.summary || {};
    const tfs = payload.timeframes || {};
    panel.innerHTML = `
      <div class="nasdq-mtf-head">
        <div><div class="nasdq-mtf-title">🧠 Top-Down Grafik Zekâsı</div><div class="nasdq-mtf-sub">1D ana yön → 1H yapı/setup → 15M giriş tetikleyicisi</div></div>
        <div class="nasdq-mtf-score"><span class="nasdq-mtf-pill ${decisionClass(s.decision)}">${esc(label(s.decision))}</span><strong>${Number(s.score || 0).toFixed(0)}/100</strong></div>
      </div>
      <div class="nasdq-mtf-grid">
        ${tfCard('1d', tfs['1d'] || {})}
        ${tfCard('1h', tfs['1h'] || {})}
        ${tfCard('15m', tfs['15m'] || {})}
      </div>
      <div class="nasdq-mtf-flow">
        <div class="nasdq-mtf-flow-item">1D Rejim<b>${esc(label(s.regime))}</b></div>
        <div class="nasdq-mtf-flow-item">1H Setup Durumu<b>${esc(label(s.setup_state))}</b></div>
        <div class="nasdq-mtf-flow-item">15M Tetik Durumu<b>${esc(label(s.trigger_state))}</b></div>
      </div>
      <div style="margin-top:.5rem;font-size:.65rem;color:#64748b">Zaman dilimi uyumu: <strong class="${decisionClass(s.alignment)}">${esc(label(s.alignment))}</strong>. 15M tek başına 1D/1H düşüş yapısını geçersiz kılamaz.</div>
      <div class="nasdq-mtf-reasons">${(payload.explanation || []).map(r => `<div class="nasdq-mtf-reason">${esc(r)}</div>`).join('')}</div>
    `;
  }

  async function load(symbol) {
    if (!symbol || symbol === 'SİSTEM') return;
    const panel = ensurePanel();
    if (!panel) return;
    const token = ++activeRequest;
    panel.innerHTML = '<div class="nasdq-mtf-loading">1D, 1H ve 15M aynı anda okunuyor...</div>';

    if (cache.has(symbol)) {
      render(cache.get(symbol));
      return;
    }

    try {
      const response = await fetch(`${MTF_API}${encodeURIComponent(symbol)}`, { cache: 'no-store' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
      if (token !== activeRequest) return;
      cache.set(symbol, payload);
      render(payload);
    } catch (error) {
      if (token !== activeRequest) return;
      panel.innerHTML = `<div class="nasdq-mtf-error">Çoklu zaman dilimi analizi yüklenemedi: ${esc(error && error.message ? error.message : error)}</div>`;
      console.error('MTF chart analysis failed', error);
    }
  }

  window.openStockModal = function(symbol) {
    if (typeof previousOpenStockModal === 'function') previousOpenStockModal(symbol);
    else {
      const modal = document.getElementById('stockModal');
      if (modal) modal.classList.add('active');
    }
    load(symbol);
  };

  window.closeStockModal = function(event) {
    activeRequest++;
    if (typeof previousCloseStockModal === 'function') return previousCloseStockModal(event);
    const modal = document.getElementById('stockModal');
    if (modal) modal.classList.remove('active');
  };

  ensureStyles();
})();
