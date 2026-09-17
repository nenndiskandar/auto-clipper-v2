// Engine Status Checker for Desktop and Mobile
// Server-side fallback: kalau field kosong, server pakai key tersimpan di config.json
async function checkEngineStatus() {
  try {
    const r = await fetch('/api/binaries');
    if (r.ok) {
      const list = await r.json();
      list.forEach(b => {
        const ok = !!b.ok;
        const colorCls = ok ? 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.6)]' : 'bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.6)]';
        const titleText = ok ? `${b.name}: Ready (${b.detail || 'ok'})` : `${b.name}: Error (${b.detail || 'failed'})`;

        ['st_' + b.name.replace('-', ''), 'st_' + b.name.replace('-', '') + '_mob'].forEach(id => {
          const el = document.getElementById(id);
          if (el) {
            el.className = `w-2 h-2 rounded-full ${colorCls} transition-all duration-300`;
            el.title = titleText;
          }
        });
      });
    }

    // AI Proxy Status
    try {
      const configRes = await fetch('/api/config');
      if (configRes.ok) {
        const config = await configRes.json();
        const testRes = await fetch('/api/test-llm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            server_url: config.server_url || '',
            hf_api_key: '' // biar server pakai key tersimpan dari config.json
          })
        });
        const testData = await testRes.json();
        const aiOk = !!testData.ok;
        const colorCls = aiOk ? 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.6)]' : 'bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.6)]';
        const titleText = aiOk ? `AI Proxy: Connected` : `AI Proxy: Disconnected`;

        ['st_ai', 'st_ai_mob'].forEach(id => {
          const el = document.getElementById(id);
          if (el) {
            el.className = `w-2 h-2 rounded-full ${colorCls} transition-all duration-300`;
            el.title = titleText;
          }
        });
      }
    } catch (e) {
      ['st_ai', 'st_ai_mob'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
          el.className = 'w-2 h-2 rounded-full bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.6)]';
          el.title = 'AI Proxy: Offline/Error';
        }
      });
    }
  } catch (e) {
    console.error('Failed to check engine status', e);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  checkEngineStatus();
  setInterval(checkEngineStatus, 15000);
});