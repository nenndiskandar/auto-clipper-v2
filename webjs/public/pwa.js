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
    if (document.getElementById('installBanner')) return;

    // Pasang banner di header / paling atas halaman index
    const b = document.createElement('div');
    b.id = 'installBanner';
    b.className = 'w-full bg-accent text-black text-xs font-bold px-4 py-2 flex items-center justify-between shadow-md z-40 relative';
    b.innerHTML = '<span>📱 Pasang aplikasi untuk pengalaman lebih baik</span><div class="flex items-center gap-2"><button id="btnInstallPwa" class="px-2 py-1 bg-black text-white rounded text-[10px] hover:bg-zinc-800">Install</button><button id="closeInstall" class="text-black/70 hover:text-black text-sm font-bold">✕</button></div>';

    const header = document.querySelector('header') || document.body;
    if (header === document.body) {
      document.body.insertBefore(b, document.body.firstChild);
    } else {
      header.parentNode.insertBefore(b, header);
    }

    document.getElementById('btnInstallPwa').addEventListener('click', () => {
      if (deferredPrompt) {
        deferredPrompt.prompt();
        deferredPrompt.userChoice.then(() => { deferredPrompt = null; b.remove(); });
      } else {
        alert('Untuk install: buka menu titik tiga browser, lalu pilih "Simpan ke Layar Utama" / "Add to Home screen".');
      }
    });

    document.getElementById('closeInstall').addEventListener('click', () => {
      b.remove();
    });
  }

  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    if (window.location.pathname === '/' || window.location.pathname === '/index.html') {
      renderInstallBanner();
    }
  });

  function updateOfflineBanner() {
    let b = document.getElementById('offlineBanner');
    if (!navigator.onLine) {
      if (!b) {
        b = document.createElement('div');
        b.id = 'offlineBanner';
        b.className = 'fixed bottom-4 right-4 z-50 bg-red-600 text-white text-xs font-semibold px-4 py-2 rounded-lg shadow-lg flex items-center gap-2 border border-red-500';
        b.innerHTML = '<span>⚠️ Tidak ada koneksi internet (Offline)</span>';
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
    if (window.location.pathname === '/' || window.location.pathname === '/index.html') {
      renderInstallBanner();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
