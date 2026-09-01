(() => {
  'use strict';

  const API = '/api/learning/';
  const CASE_API = '/api/learning-cases/';
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

  function ret(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '-';
    return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
  }

  function rate(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toFixed(1) + '%' : '-';
  }

  function outcomeLabel(value) {
    const labels = {
      TARGET_HIT: 'HEDEF',
      STOPPED_OUT: 'STOP',
      NO_ENTRY: 'GİRİŞ YOK',
      EXPIRED: 'SÜRE DOLDU',
      TRACKING: 'TAKİPTE',
      OBSERVED_ONLY: 'SADECE GÖZLEM'
    };
    return labels[value] || value || '-';
  }

  function outcomeClass(value) {
    if (value === 'TARGET_HIT') return 'learn-pos';
    if (value === 'STOPPED_OUT' || value === 'NO_ENTRY' || value === 'EXPIRED') return 'learn-neg';
    return '';
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
      .learn-card-title{font-size:.68rem;font-weight:800;color:#cbd5e1;margin-bottom:.4rem}.learn-factor{display:flex;justify-content:space-between;gap:.5rem;padding:.25rem 0;border-bottom:1px solid rgba(255,255,255,.035);font-size:.65rem;color:#94a3b8}.learn-factor:last-child{border-bottom:none}.learn-factor strong{white-space:nowrap}.learn-pos strong,.learn-pos{color:#34d399}.learn-neg strong,.learn-neg{color:#fb7185}
      .learn-guard{margin-top:.65rem;padding:.5rem .6rem;border-radius:8px;background:rgba(59,130,246,.055);border:1px solid rgba(59,130,246,.14);font-size:.65rem;line-height:1.45;color:#94a3b8}.learn-loading{padding:.8rem;text-align:center;color:#94a3b8;font-size:.72rem}.learn-error{padding:.65rem;border-radius:8px;background:rgba(239,68,68,.07);color:#fca5a5;font-size:.7rem}
      .learn-case-block{margin-top:.9rem;padding-top:.85rem;border-top:1px solid rgba(255,255,255,.08)}.learn-case-title{font-size:.78rem;font-weight:850;color:#f8fafc}.learn-case-sub{font-size:.63rem;color:#64748b;margin-top:.12rem}
      .learn-case-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.4rem;margin-top:.55rem}.learn-case-kpi{padding:.48rem;border-radius:8px;background:rgba(14,20,32,.58);border:1px solid rgba(255,255,255,.055)}
      .learn-case-list{display:grid;gap:.42rem;margin-top:.6rem}.learn-case-row{padding:.58rem;border-radius:8px;background:rgba(8,12,20,.42);border:1px solid rgba(255,255,255,.055)}.learn-case-row-head{display:flex;justify-content:space-between;gap:.5rem;font-size:.66rem;color:#cbd5e1}.learn-case-metrics{display:flex;gap:.55rem;flex-wrap:wrap;margin-top:.3rem;font-size:.62rem;color:#94a3b8}.learn-case-flags{margin-top:.28rem;font-size:.61rem;line-height:1.4;color:#fca5a5}
      .learn-wf{margin-top:.6rem;padding:.6rem;border-radius:8px;background:rgba(59,130,246,.045);border:1px solid rgba(59,130,246,.12);font-size:.64rem;color:#94a3b8}.learn-wf strong{color:#e2e8f0}.learn-setup-table{margin-top:.5rem;display:grid;gap:.25rem}.learn-setup-row{display:grid;grid-template-columns:1.2fr repeat(4,.7fr);gap:.35rem;font-size:.61rem;color:#94a3b8;padding:.25rem .15rem;border-bottom:1px solid rgba(255,255,255,.035)}.learn-setup-row strong{color:#e2e8f0}
      @media(max-width:760px){.learn-kpis,.learn-case-kpis{grid-template-columns:repeat(2,1fr)}.learn-grid{grid-template-columns:1fr}.learn-setup-row{grid-template-columns:1fr 1fr}}
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

  function renderSetupPerformance(rows) {
    if (!Array.isArray(rows) || !rows.length) return '<div style="font-size:.63rem;color:#64748b">Bu setup için henüz yeterli geçmiş vaka yok.</div>';
    return `<div class="learn-setup-table">
      <div class="learn-setup-row" style="color:#64748b"><span>Setup</span><span>Vaka</span><span>Entry</span><span>Hit</span><span>+2%</span></div>
      ${rows.slice(0,5).map(r => `<div class="learn-setup-row"><strong>${esc(r.setup_type)}</strong><span>${Number(r.cases || 0)}</span><span>${rate(r.entry_rate_pct)}</span><span>${rate(r.target_hit_rate_pct)}</span><span>${rate(r.plus_2_observed_rate_pct)}</span></div>`).join('')}
    </div>`;
  }

  function renderRecentCases(cases) {
    if (!Array.isArray(cases) || !cases.length) return '<div style="font-size:.63rem;color:#64748b">Bu hisse için düzenli seans vaka geçmişi henüz oluşmadı.</div>';
    return `<div class="learn-case-list">${cases.slice(0,4).map(c => {
      const m = c.metrics || {};
      const flags = (c.possible_mistake_flags || []).slice(0,4);
      return `<div class="learn-case-row">
        <div class="learn-case-row-head"><strong>${esc(c.case_date)} · ${esc(c.setup_type)}</strong><strong class="${outcomeClass(c.outcome)}">${esc(outcomeLabel(c.outcome))}</strong></div>
        <div class="learn-case-metrics">
          <span>MFE ${ret(m.mfe_pct)}</span><span>MAE ${ret(m.mae_pct)}</span><span>1H ${ret(m.one_hour_return_pct)}</span><span>EOD ${ret(m.eod_return_pct)}</span><span>+1 ${m.hit_plus_1_pct ? '✓' : '—'}</span><span>+2 ${m.hit_plus_2_pct ? '✓' : '—'}</span><span>Stop→Target ${c.stop_before_target ? 'STOP ÖNCE' : '—'}</span>
        </div>
        ${flags.length ? `<div class="learn-case-flags">Olası hata bağlamı: ${flags.map(esc).join(' · ')}</div>` : ''}
      </div>`;
    }).join('')}</div>`;
  }

  function renderCaseAnalytics(data) {
    if (!data || data.error) return `<div class="learn-case-block"><div class="learn-error">Vaka analitiği yüklenemedi${data && data.error ? ': ' + esc(data.error) : ''}</div></div>`;
    const s = data.summary || {};
    const wf = data.walk_forward || {};
    const folds = Array.isArray(wf.folds) ? wf.folds : [];
    const lastFold = folds.length ? folds[folds.length - 1] : null;
    return `
      <div class="learn-case-block">
        <div class="learn-case-title">📚 Vaka Defteri · MFE / MAE / 1H / EOD / Walk-Forward</div>
        <div class="learn-case-sub">Her düzenli seans adayının sonraki taramalarda ne yaptığı ölçülür. Bunlar intrabar high/low değil, scanner'ın gerçekten gördüğü fiyat örnekleridir.</div>
        <div class="learn-case-kpis">
          <div class="learn-case-kpi"><div class="learn-k">Vaka</div><div class="learn-v">${Number(s.cases || 0)}</div></div>
          <div class="learn-case-kpi"><div class="learn-k">Entry Oranı</div><div class="learn-v">${rate(s.entry_rate_pct)}</div></div>
          <div class="learn-case-kpi"><div class="learn-k">Target Hit</div><div class="learn-v">${rate(s.target_hit_rate_pct)}</div></div>
          <div class="learn-case-kpi"><div class="learn-k">Stop Önce</div><div class="learn-v">${rate(s.stop_before_target_rate_pct)}</div></div>
          <div class="learn-case-kpi"><div class="learn-k">+1% Gördü</div><div class="learn-v">${rate(s.plus_1_observed_rate_pct)}</div></div>
          <div class="learn-case-kpi"><div class="learn-k">+2% Gördü</div><div class="learn-v">${rate(s.plus_2_observed_rate_pct)}</div></div>
          <div class="learn-case-kpi"><div class="learn-k">Ort. MFE / MAE</div><div class="learn-v">${ret(s.avg_post_entry_mfe_pct)} / ${ret(s.avg_post_entry_mae_pct)}</div></div>
          <div class="learn-case-kpi"><div class="learn-k">1H / EOD Ort.</div><div class="learn-v">${ret(s.avg_one_hour_return_pct)} / ${ret(s.avg_eod_return_pct)}</div></div>
        </div>
        <div class="learn-grid">
          <div class="learn-card"><div class="learn-card-title">Son vakalar ve yaptığı hatalar</div>${renderRecentCases(data.recent_cases)}</div>
          <div class="learn-card"><div class="learn-card-title">Setup bazında gerçek performans</div>${renderSetupPerformance(data.setup_performance)}</div>
        </div>
        <div class="learn-wf"><strong>Walk-forward: ${esc(wf.stage || 'COLLECTING')}</strong> · tamamlanmış vaka ${Number(wf.completed_cases || 0)}. ${lastFold ? `Son test dilimi: train hit ${rate(lastFold.train_hit_rate_pct)} → test hit ${rate(lastFold.test_hit_rate_pct)} (${pct(lastFold.hit_rate_change_pp)}).` : esc(wf.note || 'Yeterli tamamlanmış vaka bekleniyor.')}<br><span style="color:#64748b">🔒 Bu katman geçmiş sonuçları analiz eder; canlı strateji ağırlıklarını kendi kendine değiştirmez.</span></div>
      </div>`;
  }

  function render(data, caseData) {
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
      ${renderCaseAnalytics(caseData)}
    `;
  }

  async function load(symbol) {
    if (!symbol || symbol === 'SİSTEM') return;
    const panel = ensurePanel();
    if (!panel) return;
    const token = ++requestId;
    panel.innerHTML = '<div class="learn-loading">Geçmiş tahminler, MFE/MAE, stoplar ve başarılı örnekler karşılaştırılıyor...</div>';

    const cached = cache.get(symbol);
    if (cached && Date.now() - cached.at < 60000) {
      render(cached.data, cached.caseData);
      return;
    }

    try {
      const [learningResult, caseResult] = await Promise.allSettled([
        fetch(`${API}${encodeURIComponent(symbol)}`, { cache: 'no-store' }),
        fetch(`${CASE_API}${encodeURIComponent(symbol)}`, { cache: 'no-store' })
      ]);

      if (learningResult.status !== 'fulfilled') throw learningResult.reason;
      const learningResponse = learningResult.value;
      const payload = await learningResponse.json().catch(() => ({}));
      if (!learningResponse.ok) throw new Error(payload.detail || `HTTP ${learningResponse.status}`);

      let casePayload = { error: 'endpoint unavailable' };
      if (caseResult.status === 'fulfilled') {
        const caseResponse = caseResult.value;
        casePayload = await caseResponse.json().catch(() => ({}));
        if (!caseResponse.ok) casePayload = { error: casePayload.detail || `HTTP ${caseResponse.status}` };
      } else {
        casePayload = { error: caseResult.reason && caseResult.reason.message ? caseResult.reason.message : String(caseResult.reason) };
      }

      if (token !== requestId) return;
      cache.set(symbol, { at: Date.now(), data: payload, caseData: casePayload });
      render(payload, casePayload);
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
