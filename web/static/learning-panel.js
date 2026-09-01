(() => {
  'use strict';

  const API = '/api/learning/';
  let requestId = 0;
  const cache = new Map();
  const previousOpen = window.openStockModal;
  const previousClose = window.closeStockModal;

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function pct(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '-';
    return (n >= 0 ? '+' : '') + n.toFixed(1) + ' pp';
  }

  function ensureStyles() {
    if (document.getElementById('nasdq-learning-style')) return;
    const style = document.createElement('style');
    style.id = 'nasdq-learning-style';
    style.textContent = `
      .learn-box{margin:1rem 0 0;padding:1rem;border:1px solid rgba(245,158,11,.22);border-radius:12px;background:linear-gradient(135deg,rgba(245,158,11,.07),rgba(59,130,246,.035))}
      .learn-head{display:flex;align-items:flex-start;justify-content:space-between;gap:.7rem;flex-wrap:wrap;margin-bottom:.7rem}
      .learn-title{font-size:.84rem;font-weight:850;color:#f8fafc}.learn-sub{font-size:.65rem;color:#64748b;margin-top:.12rem;max-width:720px}
      .learn-stage{font-size:.67rem;font-weight:800;padding:.28rem .5rem;border-radius:999px;border:1px solid rgba(245,158,11,.28);color:#fbbf24;background:rgba(245,158,11,.08)}
      .learn-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.45rem}.learn-kpi{padding:.55rem;border-radius:8px;background:rgba(8,12,20,.5);border:1px solid rgba(255,255,255,.06)}
      .learn-k{font-size:.6rem;text-transform:uppercase;letter-spacing:.04em;color:#64748b}.learn-v{font-family:'JetBrains Mono',monospace;font-size:.82rem;font-weight:800;color:#e2e8f0;margin-top:.15rem}
      .learn-grid{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin-top:.65rem}.learn-card{padding:.65rem;border-radius:9px;background:rgba(8,12,20,.42);border:1px solid rgba(255,255,255,.06)}
      .learn-card-title{font-size:.68rem;font-weight:800;color:#cbd5e1;margin-bottom:.4rem}.learn-factor{display:flex;justify-content:space-between;gap:.5rem;padding:.25rem 0;border-bottom:1px solid rgba(255,255,255,.035);font-size:.65rem;color:#94a3b8}.learn-factor:last-child{border-bottom:none}.learn-factor strong{white-space:nowrap}.learn-pos strong{color:#34d399}.learn-neg strong{color:#fb7185}
      .learn-guard{margin-top:.65rem;padding:.5rem .6rem;border-radius:8px;background:rgba(59,130,246,.055);border:1px solid rgba(59,130,246,.14);font-size:.65rem;line-height:1.45;color:#94a3b8}.learn-loading{padding:.8rem;text-align:center;color:#94a3b8;font-size:.72rem}.learn-error{padding:.65rem;border-radius:8px;background:rgba(239,68,68,.07);color:#fca5a5;font-size:.7rem}
      @media(max-width:760px){.learn-kpis{grid-template-columns:repeat(2,1fr)}.learn-grid{grid-template-columns:1fr}}
    `;
    document.head.appendChild(style);
  }

  function ensurePanel() {
    ensureStyles();
    const modal = document.getElementById('stockModal');
    if (!modal) return null;
    let panel = document.getElementById('nasdqLearningPanel');
    if (panel) return panel;
    const card = modal.querySelector('.modal-card');
    if (!card) return null;
    panel = document.createElement('section');
    panel.id = 'nasdqLearningPanel';
    panel.className = 'learn-box';
    panel.innerHTML = '<div class="learn-loading">Hata hafızası ve geçmiş sonuçlar okunuyor...</div>';
    card.appendChild(panel);
    return panel;
  }

  function factorRows(items, cls) {
    if (!Array.isArray(items) || !items.length) return '<div style="font-size:.65rem;color:#64748b">Henüz yeterli örnek yok.</div>';
    return items.map(f => `
      <div class="learn-factor ${cls}">
        <span>${esc(f.label)} <small style="color:#475569">n=${Number(f.samples || 0)}</small></span>
        <strong>${pct(f.lift_pp)}</strong>
      </div>`).join('');
  }

  function render(data) {
    const panel = ensurePanel();
    if (!panel) return;
    const s = data.summary || {};
    const delta = Number(data.shadow_adjustment || 0);
    const shadowScore = data.shadow_score;
    const baseline = s.baseline_hit_rate_pct;
    panel.innerHTML = `
      <div class="learn-head">
        <div>
          <div class="learn-title">🧠 Hata Hafızası · Outcome Learning</div>
          <div class="learn-sub">Sistem geçmişte hedefe giden, stop olan ve süresi dolan gerçek adayların ilk analiz koşullarını karşılaştırır. Buradaki düzeltme yalnızca SHADOW skorudur.</div>
        </div>
        <span class="learn-stage">${esc(data.stage || 'COLLECTING')}</span>
      </div>
      <div class="learn-kpis">
        <div class="learn-kpi"><div class="learn-k">Çözülmüş Örnek</div><div class="learn-v">${Number(s.resolved_samples || 0)}</div></div>
        <div class="learn-kpi"><div class="learn-k">Baz Başarı</div><div class="learn-v">${baseline == null ? '-' : Number(baseline).toFixed(1) + '%'}</div></div>
        <div class="learn-kpi"><div class="learn-k">Shadow Düzeltme</div><div class="learn-v" style="color:${delta > 0 ? '#34d399' : delta < 0 ? '#fb7185' : '#e2e8f0'}">${delta >= 0 ? '+' : ''}${delta.toFixed(2)}</div></div>
        <div class="learn-kpi"><div class="learn-k">Shadow Skor</div><div class="learn-v">${shadowScore == null ? '-' : Number(shadowScore).toFixed(1) + '/100'}</div></div>
      </div>
      <div class="learn-grid">
        <div class="learn-card"><div class="learn-card-title">✅ Bu hissede geçmişte işe yarayan koşullar</div>${factorRows(data.matched_positive, 'learn-pos')}</div>
        <div class="learn-card"><div class="learn-card-title">⚠️ Bu hissede geçmişte hata üreten koşullar</div>${factorRows(data.matched_negative, 'learn-neg')}</div>
      </div>
      <div class="learn-grid">
        <div class="learn-card"><div class="learn-card-title">📈 Sistem genelinde öğrendiği olumlu dersler</div>${factorRows(data.global_positive_lessons, 'learn-pos')}</div>
        <div class="learn-card"><div class="learn-card-title">📉 Sistem genelinde öğrendiği olumsuz dersler</div>${factorRows(data.global_negative_lessons, 'learn-neg')}</div>
      </div>
      <div class="learn-guard">🔒 <strong>Güvenlik:</strong> ${esc(data.guardrail || 'Bu katman canlı strateji ağırlıklarını otomatik değiştirmez.')} ${data.adjustment_ready ? 'Yeterli örnek bulunan faktörler shadow değerlendirmeye katılıyor.' : 'Şimdilik veri toplama aşamasında; az örnekten ders çıkarıp sistemi bozmasına izin verilmiyor.'}</div>
    `;
  }

  async function load(symbol) {
    if (!symbol || symbol === 'SİSTEM') return;
    const panel = ensurePanel();
    if (!panel) return;
    const token = ++requestId;
    panel.innerHTML = '<div class="learn-loading">Geçmiş tahminler, stoplar ve başarılı örnekler karşılaştırılıyor...</div>';

    const cached = cache.get(symbol);
    if (cached && Date.now() - cached.at < 60000) {
      render(cached.data);
      return;
    }

    try {
      const response = await fetch(`${API}${encodeURIComponent(symbol)}`, { cache: 'no-store' });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
      if (token !== requestId) return;
      cache.set(symbol, { at: Date.now(), data: payload });
      render(payload);
    } catch (error) {
      if (token !== requestId) return;
      panel.innerHTML = `<div class="learn-error">Learning Journal yüklenemedi: ${esc(error && error.message ? error.message : error)}</div>`;
      console.error('Learning journal failed', error);
    }
  }

  window.openStockModal = function(symbol) {
    if (typeof previousOpen === 'function') previousOpen(symbol);
    else {
      const modal = document.getElementById('stockModal');
      if (modal) modal.classList.add('active');
    }
    load(symbol);
  };

  window.closeStockModal = function(event) {
    requestId++;
    if (typeof previousClose === 'function') return previousClose(event);
    const modal = document.getElementById('stockModal');
    if (modal) modal.classList.remove('active');
  };

  ensureStyles();
})();
