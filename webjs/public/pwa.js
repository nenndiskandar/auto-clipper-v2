// PWA & Connection Detection
(function() {
  let deferredPrompt;

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js').catch(() => {});
    });
  }

  function isMobile() {
    return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
  }

  function isPWA() {
    return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  }

  function renderInstallBanner() {
    if (!isMobile() || isPWA()) return;

    // Pasang banner sebagai tombol pertama di sidebar menu (di atas tombol Library Sesi)
    const nav = document.getElementById('sidebarLinks');
    if (!nav || document.getElementById('btnInstallPwa')) return;

    const b = document.createElement('button');
    b.id = 'btnInstallPwa';
    b.className = 'flex items-center gap-1.5 md:gap-2.5 min-h-[28px] md:min-h-[40px] px-2 md:px-3.5 py-1 md:py-2.5 text-[10px] md:text-xs rounded-lg transition bg-[var(--accent)] text-[var(--accent-text)] font-bold shadow-sm animate-pulse';
    b.innerHTML = '<span>📱</span><span>Install App (Lebih Cepat)</span>';

    nav.prepend(b);

    b.addEventListener('click', () => {
      if (deferredPrompt) {
        deferredPrompt.prompt();
        deferredPrompt.userChoice.then(() => { deferredPrompt = null; b.remove(); });
      } else {
        alert('Ketuk ikon ⋮ di browser Anda, lalu pilih "Tambahkan ke Layar Utama" / "Add to Home screen".');
      }
    });
  }

  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    renderInstallBanner();
  });

  function updateOfflineBanner() {
    let b = document.getElementById('offlineBanner');
    if (!navigator.onLine) {
      if (!b) {
        b = document.createElement('div');
        b.id = 'offlineBanner';
        b.className = 'fixed bottom-4 right-4 z-50 bg-red-600 text-white text-xs font-semibold px-4 py-2 rounded-lg shadow-lg flex items-center gap-2 border border-red-500';
        b.innerHTML = '<span>⚠️ Offline</span>';
        document.body.appendChild(b);
      }
    } else {
      if (b) b.remove();
    }
  }

  window.addEventListener('online', updateOfflineBanner);
  window.addEventListener('offline', updateOfflineBanner);

  function init() {
    updateOfflineBanner();
    renderInstallBanner();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
