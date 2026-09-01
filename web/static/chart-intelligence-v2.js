(() => {
  'use strict';

  const API = '/api/chart-v2/';
  let symbol = null;
  let timeframe = '15m';
  let requestId = 0;
  const cache = new Map();
  const previousOpen = window.openStockModal;
  const previousClose = window.closeStockModal;

  const LABELS = {
    STRONG_CANDIDATE: 'Güçlü Aday', CANDIDATE: 'Aday', WATCH: 'Takip', AVOID: 'Kaçın',
    STRONG_BULLISH: 'Güçlü Yükseliş', BULLISH: 'Yükseliş', BEARISH: 'Düşüş', TRANSITION: 'Geçiş / Range',
    HH_HL: 'HH + HL', LH_LL: 'LH + LL', MIXED: 'Karışık',
    BULLISH_CHOCH: 'Bullish CHoCH', BEARISH_CHOCH: 'Bearish CHoCH', NONE: 'Yok',
    BREAKOUT_CONFIRMED: 'Breakout Teyitli', BREAKOUT_RETEST_HOLD: 'Retest Korundu', BREAKOUT_HOLDING: 'Kırılım Korunuyor',
    BREAKOUT_READY: 'Breakout Hazır', FALSE_BREAKOUT: 'False Breakout', FAILED_BREAKOUT: 'Failed Breakout',
    BULLISH_RSI_DIVERGENCE: 'Bullish RSI Divergence', BEARISH_RSI_DIVERGENCE: 'Bearish RSI Divergence',
    GOOD_LOCATION: 'İyi Konum', NEAR_SUPPORT: 'Desteğe Yakın', RETEST_ENTRY_ZONE: 'Retest Giriş Bölgesi',
    CHASE_RISK: 'Chase Riski', POOR_RR_LOCATION: 'Zayıf Risk/Ödül', RISING: 'Artıyor', FALLING: 'Düşüyor', FLAT: 'Yatay',
    STRONG_BULLISH_CANDLE: 'Güçlü Bullish Mum', STRONG_BEARISH_CANDLE: 'Güçlü Bearish Mum',
    BULLISH_ENGULFING: 'Bullish Engulfing', BEARISH_ENGULFING: 'Bearish Engulfing', HAMMER_REJECTION: 'Hammer / Alt Fitil Reddi',
    SHOOTING_STAR_REJECTION: 'Shooting Star / Üst Fitil Reddi', INSIDE_BAR: 'Inside Bar', OUTSIDE_BAR: 'Outside Bar',
    MOMENTUM_EXPANSION_CANDLE: 'Momentum Expansion', EXHAUSTION_RISK: 'Exhaustion Riski', DOJI: 'Doji'
  };

  function esc(v) {
    return String(v == null ? '' : v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;');
  }
  function label(v) { return LABELS[v] || v || '-'; }
  function money(v) { const n=Number(v); return Number.isFinite(n) ? '$'+(Math.abs(n)>=100?n.toFixed(2):n.toFixed(3)) : '-'; }
  function cls(v) { return (v==='STRONG_CANDIDATE'||v==='CANDIDATE')?'ci2-good':(v==='AVOID'?'ci2-bad':'ci2-warn'); }

  function styles() {
    if (document.getElementById('ci2-style')) return;
    const s=document.createElement('style'); s.id='ci2-style'; s.textContent=`
      .ci2{margin:1rem 0 0;padding:1rem;border:1px solid rgba(16,185,129,.22);border-radius:12px;background:linear-gradient(135deg,rgba(16,185,129,.06),rgba(59,130,246,.045))}
      .ci2-head{display:flex;justify-content:space-between;gap:.7rem;align-items:center;flex-wrap:wrap;margin-bottom:.75rem}.ci2-title{font-size:.84rem;font-weight:850}.ci2-sub{font-size:.64rem;color:#64748b;margin-top:.12rem}
      .ci2-actions{display:flex;align-items:center;gap:.4rem}.ci2-btn{border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.035);color:#94a3b8;border-radius:7px;padding:.32rem .55rem;font-size:.65rem;font-weight:800;cursor:pointer}.ci2-btn.active{color:#6ee7b7;border-color:rgba(16,185,129,.5);background:rgba(16,185,129,.12)}
      .ci2-pill{padding:.28rem .5rem;border-radius:999px;border:1px solid;font-size:.68rem;font-weight:850}.ci2-good{color:#34d399;border-color:rgba(16,185,129,.3);background:rgba(16,185,129,.1)}.ci2-bad{color:#fb7185;border-color:rgba(239,68,68,.3);background:rgba(239,68,68,.09)}.ci2-warn{color:#fbbf24;border-color:rgba(245,158,11,.28);background:rgba(245,158,11,.08)}
      .ci2-score{font:800 .9rem 'JetBrains Mono',monospace;color:#f8fafc}.ci2-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.45rem}.ci2-comp{padding:.52rem;border-radius:8px;background:rgba(8,12,20,.55);border:1px solid rgba(255,255,255,.055)}.ci2-k{font-size:.58rem;color:#64748b;text-transform:uppercase;letter-spacing:.04em;font-weight:750}.ci2-v{font:800 .78rem 'JetBrains Mono',monospace;margin-top:.12rem;color:#e2e8f0}
      .ci2-detail{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.5rem;margin-top:.65rem}.ci2-card{padding:.65rem;border-radius:9px;background:rgba(8,12,20,.45);border:1px solid rgba(255,255,255,.06)}.ci2-card h4{font-size:.65rem;color:#60a5fa;margin:0 0 .35rem}.ci2-row{display:flex;justify-content:space-between;gap:.4rem;font-size:.63rem;color:#94a3b8;padding:.15rem 0}.ci2-row strong{color:#e2e8f0;text-align:right}
      .ci2-patterns{display:flex;gap:.3rem;flex-wrap:wrap;margin-top:.55rem}.ci2-tag{padding:.22rem .4rem;border-radius:6px;background:rgba(139,92,246,.1);border:1px solid rgba(139,92,246,.22);color:#c4b5fd;font-size:.6rem;font-weight:750}
      .ci2-lists{display:grid;grid-template-columns:1fr 1fr;gap:.55rem;margin-top:.65rem}.ci2-list{padding:.65rem;border-radius:9px;background:rgba(8,12,20,.42);border:1px solid rgba(255,255,255,.055)}.ci2-list h4{font-size:.65rem;margin-bottom:.35rem}.ci2-list.good h4{color:#34d399}.ci2-list.bad h4{color:#fb7185}.ci2-item{font-size:.63rem;color:#94a3b8;line-height:1.4;margin:.24rem 0;padding-left:.75rem;position:relative}.ci2-item:before{content:'›';position:absolute;left:.1rem;color:#64748b}.ci2-loading,.ci2-error{padding:.8rem;text-align:center;font-size:.7rem;color:#94a3b8}.ci2-error{color:#fca5a5}
      @media(max-width:900px){.ci2-grid{grid-template-columns:repeat(2,1fr)}.ci2-detail{grid-template-columns:1fr}.ci2-lists{grid-template-columns:1fr}}
    `; document.head.appendChild(s);
  }

  function panel() {
    styles();
    const modal=document.getElementById('stockModal'); if(!modal) return null;
    let p=document.getElementById('ci2-panel'); if(p) return p;
    const card=modal.querySelector('.modal-card'); if(!card) return null;
    p=document.createElement('section'); p.id='ci2-panel'; p.className='ci2';
    card.appendChild(p); return p;
  }

  function render(data) {
    const p=panel(); if(!p) return;
    const c=data.components||{}, st=data.structure||{}, br=data.breakout||{}, mo=data.momentum||{}, vo=data.volume||{}, vl=data.volatility||{}, lo=data.location||{}, ca=data.candle||{}, tp=data.trade_plan||{};
    const components=[['Trend',c.trend],['Structure',c.structure],['Breakout',c.breakout],['Hacim',c.volume],['Momentum',c.momentum],['Mum Kalitesi',c.candle_quality],['Volatilite',c.volatility],['Trade Location',c.trade_location]];
    p.innerHTML=`
      <div class="ci2-head"><div><div class="ci2-title">🔬 Chart Intelligence V2</div><div class="ci2-sub">BOS/CHoCH · false breakout · retest · candle quality · divergence · squeeze · trade location</div></div>
        <div class="ci2-actions">${['15m','1h','1d'].map(tf=>`<button class="ci2-btn ${tf===timeframe?'active':''}" data-ci2-tf="${tf}">${tf==='1d'?'1D':tf==='1h'?'1H':'15M'}</button>`).join('')}<span class="ci2-pill ${cls(data.decision)}">${esc(label(data.decision))}</span><span class="ci2-score">${Number(data.overall_score||0).toFixed(0)}/100</span></div></div>
      <div class="ci2-grid">${components.map(([k,v])=>`<div class="ci2-comp"><div class="ci2-k">${k}</div><div class="ci2-v">${Number(v||0).toFixed(0)}/100</div></div>`).join('')}</div>
      <div class="ci2-detail">
        <div class="ci2-card"><h4>MARKET STRUCTURE</h4><div class="ci2-row"><span>Yapı</span><strong>${esc(label(st.state))}</strong></div><div class="ci2-row"><span>Bullish BOS</span><strong>${st.bullish_bos?'VAR':'-'}</strong></div><div class="ci2-row"><span>Bearish BOS</span><strong>${st.bearish_bos?'VAR':'-'}</strong></div><div class="ci2-row"><span>CHoCH</span><strong>${esc(label(st.choch))}</strong></div></div>
        <div class="ci2-card"><h4>BREAKOUT & MOMENTUM</h4><div class="ci2-row"><span>Breakout</span><strong>${esc(label(br.state))}</strong></div><div class="ci2-row"><span>Seviye</span><strong>${money(br.level)}</strong></div><div class="ci2-row"><span>RSI</span><strong>${Number(mo.rsi14||0).toFixed(1)}</strong></div><div class="ci2-row"><span>Divergence</span><strong>${esc(label(mo.divergence))}</strong></div><div class="ci2-row"><span>RVOL</span><strong>${Number(vo.rvol||0).toFixed(2)}x</strong></div></div>
        <div class="ci2-card"><h4>LOCATION & VOLATILITY</h4><div class="ci2-row"><span>Konum</span><strong>${esc(label(lo.state))}</strong></div><div class="ci2-row"><span>EMA21 Uzaklık</span><strong>${Number(lo.ema21_distance_atr||0).toFixed(2)} ATR</strong></div><div class="ci2-row"><span>Dirence Alan</span><strong>${lo.room_to_resistance_r==null?'-':Number(lo.room_to_resistance_r).toFixed(2)+'R'}</strong></div><div class="ci2-row"><span>Squeeze</span><strong>${vl.squeeze?'VAR':'-'}</strong></div><div class="ci2-row"><span>Expansion</span><strong>${vl.expansion?'VAR':'-'}</strong></div></div>
      </div>
      <div class="ci2-patterns">${(ca.patterns||[]).length?(ca.patterns||[]).map(x=>`<span class="ci2-tag">${esc(label(x))}</span>`).join(''):'<span class="ci2-tag">Belirgin mum formasyonu yok</span>'}</div>
      <div class="ci2-lists"><div class="ci2-list good"><h4>✓ Olumlu Bulgular</h4>${(data.positives||[]).length?(data.positives||[]).map(x=>`<div class="ci2-item">${esc(x)}</div>`).join(''):'<div class="ci2-item">Güçlü pozitif teyit yok.</div>'}</div><div class="ci2-list bad"><h4>⚠ Riskler</h4>${(data.risks||[]).length?(data.risks||[]).map(x=>`<div class="ci2-item">${esc(x)}</div>`).join(''):'<div class="ci2-item">Belirgin ek risk işareti yok.</div>'}</div></div>
      <div class="ci2-card" style="margin-top:.6rem"><h4>YAPISAL TRADE PLANI</h4><div class="ci2-row"><span>Entry</span><strong>${money(tp.entry)}</strong></div><div class="ci2-row"><span>Stop</span><strong>${money(tp.stop)}</strong></div><div class="ci2-row"><span>TP1 · 2R</span><strong>${money(tp.target1)}</strong></div><div class="ci2-row"><span>TP2 · 3R</span><strong>${money(tp.target2)}</strong></div></div>`;
    p.querySelectorAll('[data-ci2-tf]').forEach(b=>b.addEventListener('click',()=>{const tf=b.getAttribute('data-ci2-tf'); if(tf&&tf!==timeframe){timeframe=tf; load(symbol,tf);}}));
  }

  async function load(sym, tf='15m') {
    if(!sym||sym==='SİSTEM') return;
    symbol=sym; timeframe=tf; const id=++requestId; const p=panel(); if(!p) return;
    const key=`${sym}|${tf}`;
    p.innerHTML='<div class="ci2-loading">Derin grafik analizi hazırlanıyor...</div>';
    if(cache.has(key)){render(cache.get(key)); return;}
    try{
      const r=await fetch(`${API}${encodeURIComponent(sym)}?timeframe=${encodeURIComponent(tf)}`,{cache:'no-store'}); const d=await r.json().catch(()=>({}));
      if(!r.ok) throw new Error(d.detail||`HTTP ${r.status}`); if(id!==requestId) return; cache.set(key,d); render(d);
    }catch(e){if(id!==requestId)return; p.innerHTML=`<div class="ci2-error">Chart Intelligence V2 yüklenemedi: ${esc(e&&e.message?e.message:e)}</div>`; console.error(e);}
  }

  window.openStockModal=function(sym){ if(typeof previousOpen==='function') previousOpen(sym); else {const m=document.getElementById('stockModal');if(m)m.classList.add('active');} load(sym,'15m'); };
  window.closeStockModal=function(event){requestId++;symbol=null;if(typeof previousClose==='function')return previousClose(event);const m=document.getElementById('stockModal');if(m)m.classList.remove('active');};
  styles();
})();
