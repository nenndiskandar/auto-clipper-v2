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
    return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
  }

  function showInstallBanner() {
    if (isMobile() && !isPWA()) {
      let b = document.getElementById('installBanner');
      if (!b) {
        b = document.createElement('div');
        b.id = 'installBanner';
        b.className = 'bg-accent text-black text-xs font-bold px-4 py-2 flex items-center justify-between shadow-md cursor-pointer';
        b.innerHTML = '<span>📱 Pasang aplikasi untuk pengalaman lebih baik</span><button id="closeInstall" class="ml-2 text-black/50 hover:text-black">✕</button>';
        
        b.addEventListener('click', (e) => {
          if (e.target.id === 'closeInstall') {
            b.style.display = 'none';
          } else if (deferredPrompt) {
            deferredPrompt.prompt();
            deferredPrompt.userChoice.then(() => { deferredPrompt = null; b.style.display = 'none'; });
          } else {
            alert('Gunakan menu browser "Add to Home screen" atau "Install app"');
          }
        });
        
        document.body.insertBefore(b, document.body.firstChild);
      }
    }
  }

  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    showInstallBanner();
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
    // Only show install banner on index page if needed
    if (window.location.pathname === '/' || window.location.pathname === '/index.html') {
      showInstallBanner();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
