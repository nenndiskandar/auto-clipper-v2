/* Auto Clipper Shared UI Helpers (Toast, Confirm, Disk, Dark Mode) */

/* ── Toast Notification ── */
function showToast(message, type = 'info', duration = 3000) {
  let container = document.querySelector('.toast-container');
  if (!container) {
    container = document.createElement('div');
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  toast.className = 'toast toast-' + type;
  const icon = type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ️';
  toast.innerHTML = '<span>' + icon + '</span><span></span>';
  toast.lastElementChild.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('hide');
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

/* ── Custom Confirm Modal ── */
function showConfirm(title, message) {
  return new Promise(resolve => {
    let modal = document.querySelector('.confirm-modal');
    if (!modal) {
      modal = document.createElement('div');
      modal.className = 'confirm-modal';
      modal.innerHTML = '<div class="confirm-box">' +
        '<div class="confirm-title"></div>' +
        '<div class="confirm-msg"></div>' +
        '<div class="confirm-actions">' +
        '<button class="btn-cancel" type="button">Batal</button>' +
        '<button class="btn-confirm" type="button">Konfirmasi</button>' +
        '</div></div>';
      document.body.appendChild(modal);
      modal.querySelector('.btn-cancel').addEventListener('click', () => {
        modal.classList.remove('open'); resolve(false);
      });
      modal.querySelector('.btn-confirm').addEventListener('click', () => {
        modal.classList.remove('open'); resolve(true);
      });
      modal.addEventListener('click', e => {
        if (e.target === modal) { modal.classList.remove('open'); resolve(false); }
      });
    }
    modal.querySelector('.confirm-title').textContent = title;
    modal.querySelector('.confirm-msg').textContent = message;
    modal.classList.add('open');
  });
}

/* ── Disk Space Widget ── */
async function refreshDisk() {
  const bar = document.querySelector('.disk-fill');
  const label = document.querySelector('.disk-label');
  if (!bar || !label) return;
  try {
    const r = await fetch('/api/disk');
    const d = await r.json();
    const pct = Math.round(d.usedPercent || 0);
    const gb = v => (v / (1024 * 1024 * 1024)).toFixed(1);
    label.textContent = '💾 ' + gb(d.used) + ' / ' + gb(d.total) + ' GB (' + pct + '%)';
    bar.style.width = pct + '%';
    bar.className = 'disk-fill' + (pct >= 90 ? ' danger' : pct >= 80 ? ' warn' : '');
  } catch {
    label.textContent = '💾 disk: n/a';
  }
}

/* ── Dark Mode Toggle ── */
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === 'light') {
    root.classList.remove('dark');
    root.classList.add('light');
  } else {
    root.classList.remove('light');
    root.classList.add('dark');
  }
  try {
    localStorage.setItem('ac_theme', theme);
  } catch (e) {}
  document.querySelectorAll('.darkmode-btn').forEach(btn => {
    btn.textContent = theme === 'light' ? '☀️' : '🌙';
  });
}

function initTheme() {
  const saved = localStorage.getItem('ac_theme') || 'light';
  applyTheme(saved);
}

// Global click delegation for dark mode button (works anywhere, anytime)
document.addEventListener('click', e => {
  const btn = e.target.closest('.darkmode-btn');
  if (btn) {
    e.preventDefault();
    e.stopPropagation();
    const isLight = document.documentElement.classList.contains('light');
    const nextTheme = isLight ? 'dark' : 'light';
    applyTheme(nextTheme);
  }
});

// Immediate execution for instant theme application (prevent flash)
try {
  const initialTheme = localStorage.getItem('ac_theme') || 'light';
  applyTheme(initialTheme);
} catch (e) {}

// Lifecycle hooks
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    refreshDisk();
    setInterval(refreshDisk, 30000);
  });
} else {
  initTheme();
  refreshDisk();
  setInterval(refreshDisk, 30000);
}
