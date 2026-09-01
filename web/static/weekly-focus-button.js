(() => {
  'use strict';
  if (document.getElementById('weeklyFocusShortcut')) return;
  const a = document.createElement('a');
  a.id = 'weeklyFocusShortcut';
  a.href = '/focus';
  a.textContent = '🎯 BU HAFTANIN 20 HİSSESİ';
  Object.assign(a.style, {
    position: 'fixed', right: '18px', bottom: '18px', zIndex: '9999',
    padding: '11px 14px', borderRadius: '10px', textDecoration: 'none',
    fontSize: '12px', fontWeight: '900', letterSpacing: '.02em',
    color: '#e0f2fe', background: 'linear-gradient(135deg,#1d4ed8,#0f766e)',
    border: '1px solid rgba(147,197,253,.28)', boxShadow: '0 10px 30px rgba(0,0,0,.35)'
  });
  document.body.appendChild(a);
})();
