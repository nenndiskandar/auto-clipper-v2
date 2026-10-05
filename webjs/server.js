// auto-clipper result viewer - zero deps, node >= 16
const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFile } = require('child_process');
const { spawn } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const SESSIONS = path.join(ROOT, 'output', 'sessions');
const PUBLIC = path.join(__dirname, 'public');
const PORT = process.env.PORT || 3000;
const isWin = process.platform === 'win32';
// Resolve Python at startup: env override first, else probe candidates for a
// working Python 3 with cv2 (the pipeline's hard dependency). This keeps the
// server runnable on Windows, macOS, and Linux without per-machine edits.
const { execFileSync } = require('child_process');
function resolvePy() {
  if (process.env.CLIPPER_PY) return process.env.CLIPPER_PY;
  // 1) venv first - works on both Debian & Ubuntu, isolates deps from system python
  const venvCands = [
    path.join(ROOT, 'venv', 'bin', 'python'),
    path.join(ROOT, 'venv', 'bin', 'python3'),
    path.join(ROOT, '.venv', 'bin', 'python'),
    path.join(ROOT, '.venv', 'bin', 'python3'),
  ];
  for (const vc of venvCands) {
    if (fs.existsSync(vc)) {
      try { execFileSync(vc, ['-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'], { stdio: 'ignore' }); return vc; } catch {}
    }
  }
  // 2) system candidates - Debian (/usr/bin/python3.11) vs Ubuntu (/usr/bin/python3.10/3.12)
  // prefer one with full deps (cv2+yt_dlp), fallback to any python >=3.8
  const cands = isWin ? ['py', 'python', 'python3'] : ['/usr/bin/python3', '/usr/local/bin/python3', 'python3', 'python'];
  let fallback = null;
  for (const c of cands) {
    try {
      execFileSync(c, ['-c', 'import cv2, yt_dlp; import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'], { stdio: 'ignore' });
      return c;
    } catch {}
    try {
      execFileSync(c, ['-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'], { stdio: 'ignore' });
      if (!fallback) fallback = c;
    } catch {}
  }
  // 3) venv exists but didn't pass version check - still return it (better than system without deps)
  for (const vc of venvCands) if (fs.existsSync(vc)) return vc;
  return fallback || cands[0];
}
const PY = resolvePy();

// Resolve bundled/system ffmpeg: <ROOT>/ffmpeg/ffmpeg[.exe] first, then PATH + common Debian/Ubuntu locations
const FFMPEG = (() => {
  const bundled = path.join(ROOT, 'ffmpeg', isWin ? 'ffmpeg.exe' : 'ffmpeg');
  if (fs.existsSync(bundled)) return bundled;
  const explicit = isWin ? [] : ['/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg', '/bin/ffmpeg', '/snap/bin/ffmpeg'];
  for (const p of explicit) if (fs.existsSync(p)) return p;
  for (const dir of (process.env.PATH || '').split(path.delimiter)) {
    const full = path.join(dir, isWin ? 'ffmpeg.exe' : 'ffmpeg');
    if (fs.existsSync(full)) return full;
  }
  return 'ffmpeg';
})();

// Force Python child stdout/stderr to be line-buffered instead of block-buffered.
// Without this, helper scripts (process_session, render_clip, phase1_create,
// refind_highlights) buffer their output and process.log / the live-progress %
// only flushes when the ~8KB buffer fills -- so long steps (e.g. local Whisper
// transcription) appear "stuck" at a stale percentage until the process exits.
process.env.PYTHONUNBUFFERED = '1';

// --- Auth: password login (cookie HMAC) ---
const COOKIE_NAME = 'clipper_auth';
const BOT_TOKEN = (() => { try { return JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json'), 'utf8')).telegram_bot_token || ''; } catch { return ''; } })();
const AUTH_KEY = crypto.createHash('sha256').update('clipper-web:' + BOT_TOKEN + ':' + process.env.CLIPPER_PASS || '').digest();
const ADMIN_PASS = process.env.CLIPPER_PASS;

const signVal = v => crypto.createHmac('sha256', AUTH_KEY).update(v).digest('base64url');
const b64u = s => Buffer.from(String(s), 'utf8').toString('base64url');
const SESSION_MS = 24 * 3600 * 1000; // sesi login berlaku 24 jam
const makeToken = (id, name) => { const exp = Date.now() + SESSION_MS; const nb = b64u(name || ''); return `${id}|${exp}|${nb}|${signVal(id + '|' + exp + '|' + nb)}`; };
function checkToken(t) {
  if (!t) return null;
  const parts = String(t).split('|');
  if (parts.length !== 4) return null;
  const [id, exp, nb, sig] = parts;
  try {
    const a = Buffer.from(signVal(id + '|' + exp + '|' + nb)), b = Buffer.from(sig);
    if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  } catch { return null; }
  return Number(exp) > Date.now() ? { id, name: Buffer.from(nb, 'base64url').toString('utf8') } : null;
}
const getCookie = h => Object.fromEntries(String(h || '').split(';').map(c => c.trim().split(/=(.*)/s).slice(0, 2)).filter(p => p[0]));

// Login rate limit: maks 5 percobaan per IP per 5 menit
const LOGIN_MAX = 5;
const LOGIN_WINDOW_MS = 5 * 60 * 1000;
const LOGIN_ATTEMPTS = new Map(); // ip -> { count, resetTime }
function checkLoginLimit(ip) {
  const now = Date.now();
  let rec = LOGIN_ATTEMPTS.get(ip);
  if (!rec || now >= rec.resetTime) {
    rec = { count: 0, resetTime: now + LOGIN_WINDOW_MS };
    LOGIN_ATTEMPTS.set(ip, rec);
  }
  rec.count++;
  LOGIN_ATTEMPTS.set(ip, rec);
  // bersihkan entri kedaluwarsa biar Map tidak membesar
  if (LOGIN_ATTEMPTS.size > 1000) {
    for (const [k, v] of LOGIN_ATTEMPTS) if (now >= v.resetTime) LOGIN_ATTEMPTS.delete(k);
  }
  const remaining = Math.max(0, LOGIN_MAX - rec.count);
  return { allowed: remaining > 0, remaining, resetInSec: Math.max(0, Math.ceil((rec.resetTime - now) / 1000)) };
}

// Statistik disk partisi root (satuan byte), dipakai widget sidebar /api/disk
function getDiskStats() {
  const def = { total: 0, used: 0, free: 0, usedPercent: 0, error: null };
  return new Promise(res => {
    execFile('df', ['-B1', '/'], { timeout: 5000, env: { ...process.env, LC_ALL: 'C' } }, (err, stdout) => {
      if (err) return res({ ...def, error: String(err.message || err) });
      // locale-proof: LC_ALL=C bikin header selalu "Filesystem", tapi tetap fallback jika filter gagal
      let lines = String(stdout || '').split('\n').filter(l => l.trim());
      let line = lines.find(l => !/^Filesystem/i.test(l.trim()) && /^\//.test(l.trim()));
      if (!line) line = lines.filter(l => l.trim() && !/^Filesystem/i.test(l.trim()))[0];
      if (!line) line = lines[lines.length - 1];
      if (!line) return res({ ...def, error: 'df: output kosong' });
      const parts = line.trim().split(/\s+/);
      if (parts.length < 5) return res({ ...def, error: 'df: format tidak dikenal' });
      // jika parts[1] bukan angka (header keambil), coba ambil dari belakang
      let total, used, free;
      if (!isNaN(parseInt(parts[1], 10))) {
        total = parseInt(parts[1], 10) || 0;
        used = parseInt(parts[2], 10) || 0;
        free = parseInt(parts[3], 10) || 0;
      } else {
        const rev = parts.slice().reverse();
        free = parseInt(rev[2], 10) || 0;
        used = parseInt(rev[3], 10) || 0;
        total = parseInt(rev[4], 10) || 0;
      }
      return res({ total, used, free, usedPercent: total ? Math.round((used / total) * 100) : 0, error: null });
    });
  });
}

function fmtGB(bytes) {
  if (!bytes) return '0 GB';
  return (bytes / (1024 ** 3)).toFixed(1) + ' GB';
}
function diskBarColor(pct) {
  // hijau < 80, kuning < 90, merah >= 90 (peringatan visual sebelum penuh)
  return pct >= 90 ? 'bg-red-500' : pct >= 80 ? 'bg-yellow-400' : 'bg-emerald-500';
}
// Suntik widget ke sidebar: sebelum "Engine Status" desktop, dan sisipkan
// varian mobile di strip bawah. Halaman tanpa komentar anchor fallback ke nav.
// NOTE 2026-09-14: widget disk & dark mode sekarang sudah statis di HTML (desktop+mobile),
// jadi inject server dinonaktifkan untuk hindari duplikat.
function injectDisk(html, widget) {
  return html; // no-op: widget sudah ada di HTML statis
}
// Auto-refresh widget disk tiap 30 detik (polling /api/disk, update label + bar)
const DISK_REFRESH_JS = `<script>
(function(){
  var label=document.getElementById('diskLabel'), bar=document.querySelector('.disk-widget .h-full');
  var mob=document.getElementById('diskWidgetMob');
  if(!label && !bar) return;
  function fmt(b){ return b ? (b/1073741824).toFixed(1)+' GB' : '0 GB'; }
  function color(p){ return p>=90 ? 'bg-red-500' : p>=80 ? 'bg-yellow-400' : 'bg-emerald-500'; }
  function refresh(){
    fetch('/api/disk').then(function(r){ return r.json(); }).then(function(d){
      if(d && !d.error && d.total){
        if(label) label.textContent = fmt(d.used)+' / '+fmt(d.total);
        if(bar){ bar.style.width = Math.min(100, d.usedPercent)+'%'; bar.className = 'h-full rounded-full transition-all duration-500 '+color(d.usedPercent); }
        var vb=bar; if(mob){ var mb=mob.querySelector('.h-full'); if(mb){ mb.style.width=Math.min(100,d.usedPercent)+'%'; mb.className='h-full rounded-full transition-all duration-500 '+color(d.usedPercent); } }
      }
    }).catch(function(){});
  }
  setInterval(refresh, 30000);
})();
</script>`;
// helper render JSON sekaligus inject script refresh ke HTML
// NOTE 2026-09-14: refresh disk sekarang ditangani ui.js (polling /api/disk 30s),
// jadi inject script server dinonaktifkan.
function injectDiskScript(html) {
  return html; // no-op: ui.js sudah handle refresh disk
}

// final-output preference order inside each clip folder
const VARIANTS = ['credit.mp4', 'watermark.mp4', 'captioned.mp4', 'portrait.mp4'];

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml' };

// job render aktif: key "session/clipDir" -> { proc, code, startedAt }
const RENDER_JOBS = new Map();
// job create (phase1) & process (phase2)
let CREATE_JOB = null;
const PROCESS_JOBS = new Map();
const REFIND_JOBS = new Map();
let TRANS_JOB = null;
// job story clip & facebook upload
let STORY_JOBS = new Map(); // key "run" -> job (biar /api/tasks legible)
const FB_JOBS = new Map();
const DEP_JOBS = new Map(); // dependency download/update jobs: target -> {proc,code,startedAt,logPath}  // key "run" -> job
const CAMPAIGN_JOBS = new Map(); // campaign auto 1-click: campId -> {proc, code, startedAt, logPath, resultFile, session_id, stage, child}

function safe(seg) {
  const decoded = decodeURIComponent(seg || '');
  if (!decoded || decoded.includes('..') || decoded.includes('/') || decoded.includes('\\')) throw new Error('bad path');
  return decoded;
}

function json(res, code, obj) {
  const body = JSON.stringify(obj);
  if (!res.headersSent) {
    res.writeHead(code, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
  }
  res.end(body);
}

function hmsToSec(t) {
  const m = String(t || '').match(/^(\d+):(\d+):(\d+)/);
  return m ? (+m[1] * 3600 + +m[2] * 60 + +m[3]) : -1;
}

const normT = s => String(s || '').toLowerCase().replace(/[^a-z0-9 ]/g, ' ').replace(/\s+/g, ' ').trim();

// cari highlight: 1) overlap waktu >= 50%, 2) fallback overlap kata judul
function matchHighlight(hlList, meta) {
  if (!hlList || !hlList.length) return null;
  const cs = hmsToSec(meta.start_time), ce = hmsToSec(meta.end_time);
  if (cs >= 0 && ce > cs) {
    let best = null, bestR = 0;
    for (const h of hlList) {
      const hs = hmsToSec(h.start_time), he = hmsToSec(h.end_time);
      if (hs < 0 || he <= hs) continue;
      const ov = Math.min(ce, he) - Math.max(cs, hs);
      if (ov <= 0) continue;
      const r = ov / Math.min(ce - cs, he - hs);
      if (r > bestR) { bestR = r; best = h; }
    }
    if (bestR >= 0.5) return best;
  }
  const title = meta.title;
  const exact = hlList.find(h => normT(h.title) === normT(title));
  if (exact) return exact;
  const tw = new Set(normT(title).split(' ').filter(Boolean));
  let best = null, bestSc = 0;
  for (const h of hlList) {
    const hw = new Set(normT(h.title).split(' ').filter(Boolean));
    let inter = 0;
    hw.forEach(w => { if (tw.has(w)) inter++; });
    const sc = inter / Math.max(hw.size, tw.size);
    if (sc > bestSc) { bestSc = sc; best = h; }
  }
  return bestSc >= 0.5 ? best : null;
}

function listClips(sessionDir, highlights) {
  const cdir = path.join(sessionDir, 'clips');
  if (!fs.existsSync(cdir)) return [];
  const hlMap = new Map((highlights || []).map(h => [String(h.title || '').trim().toLowerCase(), h]));
  return fs.readdirSync(cdir)
    .filter(f => fs.statSync(path.join(cdir, f)).isDirectory())
    .sort()
    .flatMap(dir => {
      try {
        const meta = JSON.parse(fs.readFileSync(path.join(cdir, dir, 'data.json')));
        const files = fs.readdirSync(path.join(cdir, dir)).filter(f => f.toLowerCase().endsWith('.mp4')).sort();
        // ponytail: file terbaru (bukan hook/landscape) = hasil final dari semua proses
        const cands = files.filter(f => !['hook.mp4', 'landscape.mp4'].includes(f));
        const mt = f => { try { return fs.statSync(path.join(cdir, dir, f)).mtimeMs; } catch { return 0; } };
        const primary = cands.slice().sort((a, b) => mt(b) - mt(a))[0] || files[0];
        if (!primary) return [];
        let created = 0;
        for (const f of cands) created = Math.max(created, mt(f));
        const hl = matchHighlight(highlights, meta) || {};
        const sk = meta.social_kit || {};
        return [{
          dir,
          title: meta.title || dir.replace(/^\d+_/, ''),
          post_title: sk.title || '',
          hook_text: meta.hook_text || hl.hook_text || '',
          desc: sk.description || hl.description || '',
          hashtags: sk.hashtags || '',
          analysis: sk.ai_analysis || '',
          score: hl.virality_score ?? null,
          start_time: meta.start_time || hl.start_time || '',
          end_time: meta.end_time || hl.end_time || '',
          duration: meta.duration_seconds ?? hl.duration_seconds ?? '',
          start_sec: hmsToSec(meta.start_time || hl.start_time),
          transcript: hl.transcript_text || '',
          channel: meta.channel_name || '',
          aspect: meta.aspect_ratio || '',
          captions: !!meta.has_captions,
          hook: !!meta.has_hook,
          watermark: !!meta.has_watermark,
          credit: !!meta.has_credit,
          has_bgm: !!meta.has_bgm,
          has_broll: !!meta.has_broll,
          has_transition: !!meta.has_transition,
          hook_v2: !!meta.hook_v2,
          portrait_mode: meta.portrait_mode || '',
          watermark_position: meta.watermark_position || '',
          akun_tujuan: meta.akun_tujuan || '',
          tipe_akun: meta.tipe_akun || '',
          thumbnail: meta.thumbnail || '',
          file: primary,
          size_bytes: (() => { try { return fs.statSync(path.join(cdir, dir, primary)).size; } catch { return 0; } })(),
          created: created || null,
          files,
        }];
      } catch { return []; }
    });
}

function listSessions() {
  // ponytail: cache 1.5s biar polling UI nggak scan ulang semua folder sesi
  const now = Date.now();
  if (SESSIONS_CACHE.data && now - SESSIONS_CACHE.t < 1500) return SESSIONS_CACHE.data;
  const data = _listSessions();
  SESSIONS_CACHE.t = now;
  SESSIONS_CACHE.data = data;
  return data;
}

const SESSIONS_CACHE = { t: 0, data: null };
const invalidateSessions = () => { SESSIONS_CACHE.t = 0; };
const DASH_STORY_CACHE = { t: 0, data: [] };

function readCampaignBrief(sd, data) {
  try {
    const cfp = path.join(sd, 'campaign_brief.json');
    if (fs.existsSync(cfp)) return JSON.parse(fs.readFileSync(cfp, 'utf8'));
    if (data && data.campaign) return data.campaign;
  } catch {}
  return null;
}
function fmtRawSize(bytes) {
  if (bytes == null || isNaN(bytes)) return '-';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
  if (bytes < 1024*1024*1024) return (bytes/1024/1024).toFixed(1) + ' MB';
  return (bytes/1024/1024/1024).toFixed(2) + ' GB';
}
function listRawFiles(sessionDir) {
  const out = [];
  const bases = [path.join(sessionDir, '_temp'), path.join(sessionDir, '_temp_gdrive')];
  for (const base of bases) {
    try {
      if (!fs.existsSync(base)) continue;
      const stB = fs.statSync(base);
      if (!stB.isDirectory()) continue;
      const entries = fs.readdirSync(base);
      for (const f of entries) {
        const fp = path.join(base, f);
        try {
          const st = fs.statSync(fp);
          if (!st.isFile()) continue;
          if (st.size === 0) continue;
          const ext = path.extname(f).toLowerCase();
          const isVideo = /\.(mp4|mkv|webm|mov|avi|m4v|ts|m4a|mp3)$/i.test(f);
          const isSrt = /\.srt$/i.test(f);
          out.push({
            name: f,
            dir: path.basename(base),
            rel: path.basename(base) + '/' + f,
            size: st.size,
            sizeFmt: fmtRawSize(st.size),
            mtime: st.mtimeMs,
            mtimeIso: st.mtime.toISOString(),
            ext,
            isVideo,
            isSrt,
          });
        } catch {}
      }
    } catch {}
  }
  // root level source.* or *.mp4/*.srt yang ke-simpan di session folder langsung
  try {
    const rootEntries = fs.readdirSync(sessionDir);
    for (const f of rootEntries) {
      if (!/\.(mp4|mkv|webm|mov|avi|m4v|srt|vtt)$/i.test(f)) continue;
      if (f.startsWith('.')) continue;
      const fp = path.join(sessionDir, f);
      try {
        const st = fs.statSync(fp);
        if (!st.isFile() || st.size === 0) continue;
        // skip jika sudah ada di out dengan nama sama
        if (out.some(o => o.name === f && o.dir === '.')) continue;
        const ext = path.extname(f).toLowerCase();
        out.push({
          name: f,
          dir: '.',
          rel: f,
          size: st.size,
          sizeFmt: fmtRawSize(st.size),
          mtime: st.mtimeMs,
          mtimeIso: st.mtime.toISOString(),
          ext,
          isVideo: /\.(mp4|mkv|webm|mov|avi|m4v)$/i.test(f),
          isSrt: /\.srt$/i.test(f),
        });
      } catch {}
    }
  } catch {}
  out.sort((a,b) => (b.isVideo - a.isVideo) || (b.size - a.size) || (b.mtime - a.mtime));
  return out;
}
function _listSessions() {
  return fs.readdirSync(SESSIONS)
    .filter(d => fs.existsSync(path.join(SESSIONS, d, 'session_data.json')))
    .map(d => {
      try {
        const sd = path.join(SESSIONS, d);
        const data = JSON.parse(fs.readFileSync(path.join(sd, 'session_data.json')));
        const hlCount = Array.isArray(data.highlights) ? data.highlights.length : 0;
        const campaign = readCampaignBrief(sd, data);
        const clips = listClips(sd, data.highlights);
        return {
          id: d,
          url: data.url || null,
          status: data.status || 'unknown',
          title: (data.video_info && data.video_info.title) || (campaign && campaign.title) || d,
          channel: (data.video_info && data.video_info.channel) || (campaign && campaign.client_name) || '',
          created: fs.statSync(sd).mtime,
          updated: (data.completed_at || data.processing_started_at || null),
          duration: (data.video_info && data.video_info.duration) || null,
          total: clips.length,
          total_highlights: hlCount,
          clips,
          has_campaign: !!campaign,
          campaign: campaign ? {
            campaign_id: campaign.campaign_id || campaign.public_id || d.replace(/^tk_/, ''),
            title: campaign.title || null,
            client_name: campaign.client_name || null,
            client_avatar_url: campaign.client_avatar_url || null,
            thumbnail_url: campaign.thumbnail_url || null,
            total_prize: campaign.total_prize ?? null,
            current_prize: campaign.current_prize ?? campaign.total_prize ?? null,
            platform_rewards: campaign.platform_rewards || [],
            min_threshold: campaign.min_threshold ?? null,
            max_threshold: campaign.max_threshold ?? null,
            tags: campaign.tags || [],
            source_links: campaign.source_links || [],
            share_url: campaign.share_url || null,
            file_brief_url: campaign.file_brief_url || null,
            description: campaign.description || null,
            is_accumulation: !!campaign.is_accumulation,
            is_umkm: !!campaign.is_umkm,
            is_special_collab: !!campaign.is_special_collab,
            is_show_budget: campaign.is_show_budget ?? true,
          } : null,
        };
      } catch { return null; }
    })
    .filter(Boolean);
}

function sendFile(req, res, fp, download) {
  if (!fs.existsSync(fp) || !fs.statSync(fp).isFile()) return json(res, 404, { error: 'not found' });
  const size = fs.statSync(fp).size;
  const range = req.headers.range;
  const base = { 'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store' };
  // inline wajib biar browser (terutama in-app/Telegram) MEMUTAR, bukan mendownload
  base['Content-Disposition'] = download
    ? `attachment; filename="${path.basename(fp).replace(/"/g, '')}"`
    : `inline; filename="${path.basename(fp).replace(/"/g, '')}"`;
  if (range) {
    const m = range.match(/bytes=(\d*)-(\d*)/);
    let start = m[1] ? parseInt(m[1]) : 0;
    let end = m[2] ? parseInt(m[2]) : size - 1;
    end = Math.min(end, size - 1);
    res.writeHead(206, { ...base, 'Content-Range': `bytes ${start}-${end}/${size}`, 'Content-Length': end - start + 1 });
    fs.createReadStream(fp, { start, end }).pipe(res);
  } else {
    res.writeHead(200, { ...base, 'Content-Length': size });
    fs.createReadStream(fp).pipe(res);
  }
}

// cari file video beneran di folder klip; fallback ke VARIANTS kalau file
// yang diminta tidak ada (nama final bisa beda: captioned/portrait/dst).
// Mengembalikan path absolut atau null. Mencegah browser mendownload JSON 404.
function resolveClipFile(session, dir, file) {
  const base = path.join(SESSIONS, session, 'clips', dir);
  if (!base.startsWith(SESSIONS) || !fs.existsSync(base)) return null;
  const cand = p => path.join(base, p);
  if (file && fs.existsSync(cand(file)) && fs.statSync(cand(file)).isFile()) return cand(file);
  const files = fs.readdirSync(base)
    .filter(f => f.toLowerCase().endsWith('.mp4') && !['hook.mp4', 'landscape.mp4'].includes(f));
  const byVariant = VARIANTS.find(v => files.includes(v));
  if (byVariant) return cand(byVariant);
  if (files.length) {
    return files.slice().sort((a, b) => fs.statSync(cand(b)).mtimeMs - fs.statSync(cand(a)).mtimeMs)[0];
  }
  return null;
}

// ponytail: baca ekor file doang, bukan seluruh log
function lastLogLine(p) {
  try {
    const st = fs.statSync(p);
    if (!st.isFile() || !st.size) return '';
    const len = Math.min(st.size, 4096);
    const buf = Buffer.alloc(len);
    const fd = fs.openSync(p, 'r');
    fs.readSync(fd, buf, 0, len, st.size - len);
    fs.closeSync(fd);
    const lines = buf.toString('utf8').split('\n').filter(l => l.trim());
    return (lines[lines.length - 1] || '').slice(-300);
  } catch { return ''; }
}

function tailFile(fp, max = 12000) {
  try {
    const stat = fs.existsSync(fp) ? fs.statSync(fp).size : 0;
    if (!stat) return '';
    const fd = fs.openSync(fp, 'r');
    const buf = Buffer.alloc(Math.min(stat, max));
    fs.readSync(fd, buf, 0, buf.length, Math.max(0, stat - buf.length));
    fs.closeSync(fd);
    return buf.toString('utf8');
  } catch { return ''; }
}

// Ekstrak persen progres terakhir dari log (format: "... (overall: 42.5%)")
// Cocok untuk clip_progress (process/render) maupun [progress] (create/refind).
function parseOverall(logText) {
  if (!logText) return null;
  let m, last = null;
  const re = /overall:\s*([\d.]+)/g;
  while ((m = re.exec(logText)) !== null) last = parseFloat(m[1]);
  if (last === null || isNaN(last)) return null;
  return Math.max(0, Math.min(100, last));
}

const isLocalAddr = a => ['127.0.0.1','::1','::ffff:127.0.0.1','localhost'].includes(String(a).replace(/^::ffff:/, ''));
const isLocalRequest = req => isLocalAddr(req.connection.remoteAddress);

const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  const p = u.pathname;
  try {
    // --- public routes (no auth) ---
    // login page & root are served as static files (handled below)
    // auth endpoint
    if (p === '/api/auth/login' && req.method === 'POST') {
      const ip = req.connection.remoteAddress || 'unknown';
      const lim = checkLoginLimit(ip);
      // batasi percobaan sebelum dicek: 429 + sisa percobaan supaya UI bisa tampil
      if (!lim.allowed) {
        return json(res, 429, { error: `Terlalu banyak percobaan. Coba lagi dalam ${Math.ceil(lim.resetInSec / 60)} menit.`, remaining: 0, resetInSec: lim.resetInSec });
      }
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        try {
          const { password } = JSON.parse(body || '{}');
          if (String(password) === ADMIN_PASS) {
            const token = makeToken('admin', 'admin');
            res.writeHead(200, { 'Set-Cookie': `${COOKIE_NAME}=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${Math.floor(SESSION_MS / 1000)}` });
            return json(res, 200, { ok: true });
          }
          return json(res, 401, { error: 'Password salah', remaining: Math.max(0, LOGIN_MAX - (LOGIN_ATTEMPTS.get(ip) || { count: 0 }).count) });
        } catch(e) { return json(res, 400, { error: 'invalid request' }); }
      });
      return;
    }
    // GET /api/disk - statistik disk (total/used/free/usedPercent dalam byte) untuk widget sidebar
    if (p === '/api/disk' && req.method === 'GET') {
      getDiskStats().then(st => json(res, st.error ? 500 : 200, st));
      return;
    }

    // GET /api/proxy/ternakklip (deprecated, use /api/ternakklip-campaigns for pagination)
    if (p === '/api/proxy/ternakklip' && req.method === 'GET') {
      const https = require('https');
      const reqOpts = {
        hostname: 'api.ternakklip.com',
        path: '/api/v1/public/campaigns?limit=50',
        method: 'GET',
        headers: {
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
      };
      const proxyReq = https.request(reqOpts, (proxyRes) => {
        let resBody = '';
        proxyRes.on('data', chunk => resBody += chunk);
        proxyRes.on('end', () => {
          try {
            const jsonParsed = JSON.parse(resBody);
            json(res, 200, jsonParsed);
          } catch (e) {
            json(res, 500, { error: 'Invalid JSON from TernakKlip: ' + resBody.slice(0, 100) });
          }
        });
      });
      proxyReq.on('error', e => json(res, 500, { error: e.message }));
      proxyReq.end();
      return;
    }

    // GET /api/ternakklip-campaigns?page=1&limit=36&search=...&category=... - paginated proxy untuk ternaklip.html
    if (p.startsWith('/api/ternakklip-campaigns') && req.method === 'GET') {
      const qPage = Math.max(1, parseInt(u.searchParams.get('page') || '1', 10));
      const qLimit = Math.max(1, Math.min(100, parseInt(u.searchParams.get('limit') || '36', 10)));
      const qSearch = (u.searchParams.get('search') || u.searchParams.get('q') || '').trim().slice(0, 80);
      const qCategory = (u.searchParams.get('category') || '').trim().slice(0, 20);
      const https = require('https');
      let upstreamPath = `/api/v1/public/campaigns?limit=${qLimit}&page=${qPage}`;
      if (qSearch) upstreamPath += `&search=${encodeURIComponent(qSearch)}`;
      if (qCategory) upstreamPath += `&category=${encodeURIComponent(qCategory)}`;
      const reqOpts = {
        hostname: 'api.ternakklip.com',
        path: upstreamPath,
        method: 'GET',
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' }
      };
      const proxyReq = https.request(reqOpts, (proxyRes) => {
        let resBody = '';
        proxyRes.on('data', chunk => resBody += chunk);
        proxyRes.on('end', () => {
          try {
            const jsonParsed = JSON.parse(resBody);
            json(res, 200, {
              status: 'success',
              data: jsonParsed.data || [],
              meta: {
                page: qPage,
                limit: qLimit,
                total: jsonParsed.meta?.total_data ?? jsonParsed.meta?.total ?? (jsonParsed.data || []).length,
                total_pages: jsonParsed.meta?.total_pages ?? Math.ceil((jsonParsed.meta?.total_data ?? (jsonParsed.data || []).length) / qLimit)
              }
            });
          } catch (e) {
            json(res, 500, { error: 'Invalid JSON from TernakKlip: ' + resBody.slice(0, 100) });
          }
        });
      });
      proxyReq.on('error', e => json(res, 500, { error: e.message }));
      proxyReq.end();
      return;
    }

    // GET /api/ternakklip-campaign-detail/:id - full campaign detail (brief, source_links, dll)
    const mDetail = p.match(/^\/api\/ternakklip-campaign-detail\/([^/]+)$/);
    if (mDetail && req.method === 'GET') {
      const campId = encodeURI(mDetail[1]);
      const https = require('https');
      const reqOpts = {
        hostname: 'api.ternakklip.com',
        path: `/api/v1/public/campaigns/${campId}`,
        method: 'GET',
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' }
      };
      const proxyReq = https.request(reqOpts, (proxyRes) => {
        let resBody = '';
        proxyRes.on('data', chunk => resBody += chunk);
        proxyRes.on('end', () => {
          try {
            const jsonParsed = JSON.parse(resBody);
            if (jsonParsed.status !== 'success' || !jsonParsed.data) {
              return json(res, jsonParsed.status === 'error' ? 400 : 500, jsonParsed);
            }
            json(res, 200, { status: 'success', data: jsonParsed.data });
          } catch (e) {
            json(res, 500, { error: 'Invalid JSON from TernakKlip: ' + resBody.slice(0, 100) });
          }
        });
      });
      proxyReq.on('error', e => json(res, 500, { error: e.message }));
      proxyReq.end();
      return;
    }

    if (p === '/logout') { res.writeHead(302, { Location: '/', 'Set-Cookie': `${COOKIE_NAME}=; Path=/; Max-Age=0` }); return res.end(); }
    // --- public routes: video stream & download (tanpa auth) ---
    const mVidPub = p.match(/^\/(video|download)\/([^/]+)\/(.+)$/);
    if (mVidPub) {
      const parts = mVidPub[3].split('/').map(safe);
      if (parts.length !== 2) return json(res, 400, { error: 'bad path' });
      const fp = resolveClipFile(safe(mVidPub[2]), parts[0], parts[1]);
      if (!fp) { res.writeHead(404, { 'Content-Type': 'video/mp4' }); return res.end(); }
      return sendFile(req, res, fp, mVidPub[1] === 'download');
    }
    // /video/story/:clip/:file - stream hasil Story Clip (output/story_clips)
    const mVidStory = p.match(/^\/video\/story\/([^/]+)\/(.+)$/);
    if (mVidStory) {
      const clipDir = path.join(ROOT, 'output', 'story_clips', safe(mVidStory[1]));
      const file = path.basename(mVidStory[2]);
      const fp = path.join(clipDir, file);
      if (!clipDir.startsWith(ROOT) || !fs.existsSync(fp) || !fs.statSync(fp).isFile()) { res.writeHead(404, { 'Content-Type': 'video/mp4' }); return res.end(); }
      return sendFile(req, res, fp, false);
    }
    // /raw/:session/:rel - stream file mentahan (_temp / _temp_gdrive / root mp4/srt) with range
    const mRaw = p.match(/^\/raw\/([^/]+)\/(.+)$/);
    if (mRaw) {
      try {
        const sess = safe(mRaw[1]);
        const relRaw = decodeURIComponent(mRaw[2]);
        // block traversal - only allow names without .. and restrict to known dirs
        if (relRaw.includes('..') || relRaw.includes('\\')) return json(res, 400, { error: 'bad path' });
        const cleanParts = relRaw.split('/').filter(Boolean).map(v => {
          const d = decodeURIComponent(v);
          if (d.includes('..') || d.includes('/') || d.includes('\\')) throw new Error('bad');
          return d;
        });
        // allow _temp/xxx, _temp_gdrive/xxx, or root file xxx
        if (cleanParts.length > 2) return json(res, 400, { error: 'bad path' });
        if (cleanParts.length === 2 && !['_temp','_temp_gdrive'].includes(cleanParts[0])) return json(res, 400, { error: 'bad path' });
        const fp = path.join(SESSIONS, sess, ...cleanParts);
        if (!fp.startsWith(path.join(SESSIONS, sess) + path.sep) && fp !== path.join(SESSIONS, sess, cleanParts[0])) return json(res, 400, { error: 'bad path' });
        if (!fs.existsSync(fp) || !fs.statSync(fp).isFile()) { res.writeHead(404, { 'Content-Type': 'text/plain' }); return res.end('not found'); }
        const isDl = u.searchParams.get('download') === '1';
        // choose mime
        const ext = path.extname(fp).toLowerCase();
        let mime = 'application/octet-stream';
        if (/\.(mp4|m4v|mov)$/i.test(ext)) mime = 'video/mp4';
        else if (/\.webm$/i.test(ext)) mime = 'video/webm';
        else if (/\.mkv$/i.test(ext)) mime = 'video/x-matroska';
        else if (/\.mp3$/i.test(ext)) mime = 'audio/mpeg';
        else if (/\.m4a$/i.test(ext)) mime = 'audio/mp4';
        else if (ext === '.srt' || ext === '.vtt') mime = 'text/plain; charset=utf-8';
        res.setHeader('Content-Type', mime);
        return sendFile(req, res, fp, isDl);
      } catch (e) { return json(res, 400, { error: 'bad path' }); }
    }

    // --- Auth middleware ---
    const isLocal = isLocalRequest(req);
    const cookie = getCookie(req.headers.cookie);
    const authUser = !isLocal && cookie[COOKIE_NAME] ? checkToken(cookie[COOKIE_NAME]) : null;
    const isAuthenticated = isLocal || !!authUser;
    if (!isAuthenticated && p !== '/login.html') {
      const ext = path.extname(p);
      const isApi = p.startsWith('/api/');
      const isHtml = !ext || ext === '.html';
      if (isApi) return json(res, 401, { error: 'unauthorized' });
      if (isHtml) { res.writeHead(302, { Location: '/login.html' }); return res.end(); }
    }

    if (p === '/api/me') {
      if (isLocalRequest(req)) return json(res, 200, { id: 'local', name: 'admin' });
      const cookie = getCookie(req.headers.cookie);
      const user = checkToken(cookie[COOKIE_NAME]);
      if (user) return json(res, 200, { id: user.id, name: user.name });
      return json(res, 401, { error: 'unauthorized' });
    }
    if (p === '/api/sessions') return json(res, 200, listSessions());
    // GET /api/sessions/:id - detail satu sesi + campaign brief (sinkron ternakklip)
    const mSess = p.match(/^\/api\/sessions\/([^/]+)$/);
    if (mSess && req.method === 'GET') {
      const sid = safe(mSess[1]);
      const sd = path.join(SESSIONS, sid);
      if (!sd.startsWith(SESSIONS) || !fs.existsSync(path.join(sd, 'session_data.json'))) return json(res, 404, { error: 'session not found' });
      try {
        const data = JSON.parse(fs.readFileSync(path.join(sd, 'session_data.json'), 'utf8'));
        const campaign = readCampaignBrief(sd, data);
        const clips = listClips(sd, data.highlights);
        // ringkasan log (tail) biar frontend bisa tampil tanpa fetch terpisah
        let procLog = '', refindLog = '';
        try { procLog = tailFile(path.join(sd, 'process.log'), 6000); } catch {}
        try { refindLog = tailFile(path.join(sd, 'refind.log'), 3000); } catch {}
        // coba cari campaign log auto
        let campaignLog = '';
        try {
          const outDir = path.join(ROOT, 'output');
          const cands = fs.readdirSync(outDir).filter(f => f.startsWith('campaign_auto_') && f.includes(sid.replace(/^tk_/, '')));
          if (cands.length) campaignLog = tailFile(path.join(outDir, cands.sort().pop()), 6000);
        } catch {}
        const hlCount = Array.isArray(data.highlights) ? data.highlights.length : 0;
        // estimasi durasi & stats
        const srtFile = fs.readdirSync(sd).find(f => f.endsWith('.srt'));
        let srtExists = !!srtFile, srtSize = 0;
        try { if (srtFile) srtSize = fs.statSync(path.join(sd, srtFile)).size; } catch {}
        // file sumber original bila GDrive cache + rawFiles mentahan
        let hasTempVideo = false;
        let rawFiles = [];
        try { rawFiles = listRawFiles(sd); hasTempVideo = rawFiles.some(f => f.isVideo); } catch {}
        try { if (!hasTempVideo) hasTempVideo = fs.existsSync(path.join(sd, '_temp')) && fs.readdirSync(path.join(sd, '_temp')).some(f => /\.(mp4|mkv|webm)$/i.test(f)); } catch {}
        return json(res, 200, {
          id: sid,
          url: data.url || null,
          srt_path: data.srt_path || (srtFile ? path.join(sd, srtFile) : null),
          status: data.status || 'unknown',
          title: (data.video_info && data.video_info.title) || (campaign && campaign.title) || sid,
          channel: (data.video_info && data.video_info.channel) || (campaign && campaign.client_name) || '',
          video_info: data.video_info || null,
          created_at: data.created_at || null,
          processing_started_at: data.processing_started_at || null,
          completed_at: data.completed_at || null,
          created: (() => { try { return fs.statSync(sd).mtime; } catch { return null; } })(),
          total: clips.length,
          total_highlights: hlCount,
          highlights: data.highlights || [],
          clips,
          rawFiles,
          campaign: campaign ? {
            campaign_id: campaign.campaign_id || campaign.public_id || sid.replace(/^tk_/, ''),
            public_id: campaign.public_id || campaign.campaign_id || sid.replace(/^tk_/, ''),
            title: campaign.title || null,
            client_name: campaign.client_name || null,
            client_avatar_url: campaign.client_avatar_url || null,
            thumbnail_url: campaign.thumbnail_url || null,
            description: campaign.description || null,
            file_brief_url: campaign.file_brief_url || null,
            share_url: campaign.share_url || null,
            total_prize: campaign.total_prize ?? null,
            current_prize: campaign.current_prize ?? campaign.total_prize ?? null,
            platform_rewards: campaign.platform_rewards || [],
            min_threshold: campaign.min_threshold ?? null,
            max_threshold: campaign.max_threshold ?? null,
            tags: campaign.tags || [],
            tags_raw: campaign.tags_raw || [],
            source_links: campaign.source_links || [],
            platform: campaign.platform || [],
            language: campaign.language || [],
            is_accumulation: !!campaign.is_accumulation,
            is_umkm: !!campaign.is_umkm,
            is_special_collab: !!campaign.is_special_collab,
            is_show_budget: campaign.is_show_budget ?? true,
            total_participants: campaign.total_participants ?? null,
            created_at: campaign.created_at || null,
            updated_at: campaign.updated_at || null,
          } : null,
          has_campaign: !!campaign,
          srt_exists: srtExists, srt_size: srtSize,
          has_temp_video: hasTempVideo,
          rawFiles,
          logs: { process: procLog.slice(-4000), refind: refindLog.slice(-3000), campaign: campaignLog.slice(-4000) },
        });
      } catch (e) { return json(res, 500, { error: 'read error: ' + String(e.message || e) }); }
    }
    // GET /api/sessions/:session/clip/:clipDir - detail 1 klip (ringan, tanpa scan semua sesi)
    const mClip = p.match(/^\/api\/sessions\/([^/]+)\/clip\/([^/]+)$/);
    if (mClip) {
      const sessId = safe(mClip[1]), clipId = safe(mClip[2]);
      const sd = path.join(SESSIONS, sessId);
      if (!sd.startsWith(SESSIONS) || !fs.existsSync(sd)) return json(res, 404, { error: 'session not found' });
      try {
        const sdata = JSON.parse(fs.readFileSync(path.join(sd, 'session_data.json'), 'utf8'));
        const allClips = listClips(sd, sdata.highlights);
        const clip = allClips.find(c => c.dir === clipId);
        if (!clip) return json(res, 404, { error: 'clip not found' });
        return json(res, 200, {
          session: {
            id: sessId,
            url: sdata.url || null,
            title: (sdata.video_info && sdata.video_info.title) || sessId,
            channel: (sdata.video_info && sdata.video_info.channel) || '',
          },
          clip,
        });
      } catch { return json(res, 500, { error: 'read error' }); }
    }
    // POST /api/campaign/process/:id - Inisialisasi Master Sesi TernakKlip (legacy, cepat)
    if (p.startsWith('/api/campaign/process/') && req.method === 'POST' && !p.startsWith('/api/campaign/auto')) {
      const campId = p.split('/')[4];
      const proc = spawn(PY, [path.join(__dirname, 'phase1_campaign.py'), campId]);
      let out = '';
      proc.stdout.on('data', d => out += d);
      proc.on('close', code => {
        try {
          const j = JSON.parse(out);
          if (code === 0 && j.ok) {
            json(res, 200, { ok: true, session_id: j.session_id });
          } else {
            json(res, 500, { error: j.error || 'Gagal menyiapkan sesi campaign' });
          }
        } catch (e) {
          json(res, 500, { error: 'Format output salah: ' + out });
        }
      });
      return;
    }
    // POST /api/campaign/auto/:id + alias /api/campaign/ai-create/:id - One-click TernakKlip -> brief AI + highlight + auto-render (1b)
    if ((p.startsWith('/api/campaign/auto/') || p.startsWith('/api/campaign/ai-create/')) && req.method === 'POST' && !p.includes('/status') && !p.includes('/cancel')) {
      const segs = p.split('/').filter(Boolean);
      const campId = segs[3]; // api/campaign/auto/:id
      if (!campId) return json(res, 400, { error: 'campaign id required' });
      const prev = CAMPAIGN_JOBS.get(campId);
      if (prev && prev.code === undefined) return json(res, 409, { error: 'Campaign ini masih diproses', stage: prev.stage });
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        let o = {};
        try { o = JSON.parse(body || '{}'); } catch {}
        const preset = String(o.preset || o.template || 'tiktok_viral').trim() || 'tiktok_viral';
        const numClipsRaw = String(o.num_clips ?? o.numClips ?? 'auto').trim();
        const numClips = numClipsRaw;
        const autoRender = o.auto_render !== false && o.autoRender !== false;
        const topN = Math.max(1, Math.min(10, parseInt(o.top_n ?? o.topN ?? '3', 10) || 3));
        const logPath = path.join(ROOT, 'output', `campaign_auto_${campId}_${Date.now()}.log`);
        const resultFile = path.join(ROOT, 'output', `.campaign_auto_${campId}_${Date.now()}.json`);
        try {
          const olds = fs.readdirSync(path.join(ROOT, 'output')).filter(f => /^campaign_auto_.*\.log$/.test(f)).sort();
          while (olds.length >= 20) fs.unlinkSync(path.join(ROOT, 'output', olds.shift()));
        } catch {}
        try { fs.mkdirSync(path.join(ROOT, 'output'), { recursive: true }); } catch {}
        fs.writeFileSync(logPath, `===== campaign auto ${campId} preset=${preset} num_clips=${numClips} topN=${topN} autoRender=${autoRender} ${new Date().toISOString()} =====\n`);
        const job = { campId, preset, numClips, topN, autoRender, stage: 'campaign', code: undefined, startedAt: Date.now(), logPath, resultFile, session_id: `tk_${campId}`, sessionDir: path.join(SESSIONS, `tk_${campId}`), proc: null, count: 0 };
        CAMPAIGN_JOBS.set(campId, job);
        const campProc = spawn(PY, [path.join(__dirname, 'phase1_campaign.py'), campId]);
        job.proc = campProc;
        let outBuf = '';
        campProc.stdout.on('data', d => { outBuf += d; try { fs.appendFileSync(logPath, d); } catch {} });
        campProc.stderr.on('data', d => { try { fs.appendFileSync(logPath, d); } catch {} });
        campProc.on('close', code => {
          if (code !== 0) {
            job.code = code; job.finishedAt = Date.now(); job.stage = 'failed';
            try { fs.writeFileSync(resultFile, JSON.stringify({ ok: false, stage: 'campaign', error: (outBuf || '').slice(-1500) || 'Campaign setup gagal' })); } catch {}
            return;
          }
          let j = null;
          let jsonLine = null;
          try {
            const lines = outBuf.split('\n').map(l=>l.trim()).filter(l=>l.startsWith('{') && l.endsWith('}'));
            if (lines.length) jsonLine = lines[lines.length-1];
            else {
              const m = outBuf.match(/\{[\s\S]*"ok"[\s\S]*\}/);
              if (m) jsonLine = m[0];
            }
            if (jsonLine) j = JSON.parse(jsonLine);
            else {
              // try last JSON object via bracket counting (fallback)
              const lastBrace = outBuf.lastIndexOf('}');
              const firstBrace = outBuf.indexOf('{');
              if (firstBrace!==-1 && lastBrace!==-1) {
                const cand = outBuf.slice(firstBrace, lastBrace+1);
                // find the last complete JSON starting from the end that parses
                for (let i=cand.lastIndexOf('{'); i>=0; i=cand.lastIndexOf('{', i-1)) {
                  try { const c=JSON.parse(cand.slice(i)); if (c && typeof c.ok!=='undefined') { jsonLine=cand.slice(i); j=c; break; } } catch {}
                  if (i<=0) break;
                }
              }
            }
          } catch { j = null; }
          if (!j || !j.ok) {
            job.code = 1; job.stage = 'failed'; job.finishedAt = Date.now();
            const errPreview = (outBuf || '').replace(/\n/g,' | ').slice(-700);
            const errMsg = (j && j.error) || (jsonLine ? 'Campaign JSON ok!=true' : ('No JSON in output: ' + errPreview));
            try { fs.writeFileSync(resultFile, JSON.stringify({ ok: false, stage: 'campaign', error: errMsg })); } catch {}
            try { fs.appendFileSync(logPath, `\n[ERROR] Campaign JSON invalid: ${errPreview}\n`); } catch {}
            return;
          }
          const sessionId = j.session_id || `tk_${campId}`;
          const sessionDir = path.join(SESSIONS, sessionId);
          job.session_id = sessionId; job.sessionDir = sessionDir; job.stage = 'brief';
          // --- Brief AI: parse brief.pdf -> ai_brief.json (fallback deskripsi) ---
          let aiBrief = null;
          let bgmPath = null;
          try {
            try { fs.appendFileSync(logPath, `[INFO] Brief AI parsing (brief.pdf -> AI highlights/hashtag/durasi)...\n`); } catch {}
            const briefOut = require('child_process').execFileSync(PY, [path.join(__dirname, 'brief_processor.py'), sessionDir], { encoding: 'utf-8', timeout: 180000, maxBuffer: 1024*1024*4 });
            try { fs.appendFileSync(logPath, `[INFO] Brief AI out: ${String(briefOut).slice(0, 600)}\n`); } catch {}
            try { aiBrief = JSON.parse(fs.readFileSync(path.join(sessionDir, 'ai_brief.json'), 'utf8')); } catch {}
            if (aiBrief) {
              try { fs.appendFileSync(logPath, `[INFO] Brief AI ok dur=${aiBrief.duration_min}-${aiBrief.duration_max} req=${(aiBrief.hashtags_required||[]).join(' ')} sug=${(aiBrief.hashtags_suggested||[]).join(' ')} music=${aiBrief.music_url || aiBrief.music_query || '-'} hook=${(aiBrief.hook_wajib||'').slice(0,40) || '-'} source=${aiBrief.brief_source||'-'}\n`); } catch {}
            }
          } catch (e) {
            try { fs.appendFileSync(logPath, `[WARN] Brief AI gagal: ${String(e.message||e).slice(0, 300)}\n`); } catch {}
            try { aiBrief = JSON.parse(fs.readFileSync(path.join(sessionDir, 'ai_brief.json'), 'utf8')); } catch {}
          }
          // --- Backsound: TikTok sound cache (WORKFLOW step 3) ---
          // TikTok sound_id -> output/bgm/<id>.mp3 cache, miss -> yt-dlp --cookies --impersonate chrome -x --audio-format mp3 <canonical>
          try {
            const musicUrl = aiBrief && (aiBrief.music_url || null);
            const musicQuery = aiBrief && (aiBrief.music_query || null);
            const targetMp3 = path.join(sessionDir, '_temp', 'music.mp3');
            const bgmCacheDir = path.join(ROOT, 'output', 'bgm');
            const cookiesCandidates = [path.join(ROOT,'cookies.txt')];
            let cookiesPath = null; for (const c of cookiesCandidates) { try { if (fs.existsSync(c) && fs.statSync(c).size>0) { cookiesPath=c; break; } } catch {} }
            const ensureTemp = () => { try { fs.mkdirSync(path.join(sessionDir,'_temp'), {recursive:true}); } catch {} };
            const ensureBgmCache = () => { try { fs.mkdirSync(bgmCacheDir, {recursive:true}); } catch {} };
            const isTikTokUrl = (u)=> /tiktok\.com/i.test(String(u||'')) || /vt\.tiktok\.com|vm\.tiktok\.com/i.test(String(u||''));
            const extractTikTokId = (u)=> { const m = String(u||'').match(/\/video\/(\d{8,})/); return m ? m[1] : null; };
            const resolveTikTokCanonical = (shortUrl)=>{
              try {
                const pyCode = "import sys, urllib.request\nurl=sys.argv[1]\ntry:\n  req=urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'})\n  with urllib.request.urlopen(req, timeout=10) as r:\n    print(r.geturl())\nexcept Exception as e:\n  print(url)\n";
                const out = require('child_process').execFileSync(PY, ['-c', pyCode, String(shortUrl)], {encoding:'utf-8', timeout:12000}).trim();
                if (out && /tiktok\.com\/@[^\/]+\/video\/\d+/.test(out)) return out;
                return out || shortUrl;
              } catch { return shortUrl; }
            };
            if (musicUrl) {
              const tikIdFromUrl = extractTikTokId(musicUrl);
              const isTik = isTikTokUrl(musicUrl);
              if (isTik) {
                let cacheHit = null;
                if (tikIdFromUrl) {
                  const cached = path.join(bgmCacheDir, `${tikIdFromUrl}.mp3`);
                  if (fs.existsSync(cached) && fs.statSync(cached).size>1000) cacheHit = cached;
                }
                if (!cacheHit) {
                  try {
                    const canonicalPre = resolveTikTokCanonical(String(musicUrl));
                    const preId = extractTikTokId(canonicalPre);
                    if (preId) {
                      const cached2 = path.join(bgmCacheDir, `${preId}.mp3`);
                      if (fs.existsSync(cached2) && fs.statSync(cached2).size>1000) cacheHit = cached2;
                    }
                  } catch {}
                }
                if (cacheHit) {
                  ensureTemp();
                  try { fs.copyFileSync(cacheHit, targetMp3); bgmPath = targetMp3; } catch(e2) { try { fs.appendFileSync(logPath, `[WARN] Cache copy gagal: ${String(e2.message||e2).slice(0,120)}\n`);} catch{} }
                  if (bgmPath) try { fs.appendFileSync(logPath, `[INFO] Backsound cache HIT ${cacheHit} -> ${targetMp3} ${(fs.statSync(bgmPath).size/1024).toFixed(0)}KB (reuse, no download)\n`);} catch{}
                }
                if (!bgmPath) {
                  try { fs.appendFileSync(logPath, `[INFO] Backsound TikTok cache MISS, download music_url: ${String(musicUrl).slice(0,80)}\n`);} catch{}
                  ensureTemp(); ensureBgmCache();
                  let canonical = String(musicUrl);
                  if (/vt\.tiktok\.com|vm\.tiktok\.com/i.test(canonical) || !/\/video\/\d+/.test(canonical)) {
                    try { canonical = resolveTikTokCanonical(canonical); try{fs.appendFileSync(logPath, `[INFO] TikTok resolved canonical: ${canonical.slice(0,90)}\n`);}catch{} } catch{}
                  }
                  const idForCache = extractTikTokId(canonical) || tikIdFromUrl || 'tiktok_sound';
                  const cacheOut = path.join(bgmCacheDir, `${idForCache}.mp3`);
                  const args = ['-m','yt_dlp','-x','--audio-format','mp3','--audio-quality','0','--no-playlist','--no-warnings','--impersonate','chrome','-o', cacheOut.replace(/\.mp3$/,'.%(ext)s'), String(canonical)];
                  if (cookiesPath) { args.splice(args.length-1, 0, '--cookies', cookiesPath); }
                  try { const dp = require('child_process').execFileSync(PY, ['-c','from utils.helpers import get_deno_path;import pathlib;print(get_deno_path() or "")'], {encoding:'utf-8', timeout:4000, cwd: ROOT}).trim(); if (dp && fs.existsSync(dp)) { args.splice(args.length-1, 0, '--js-runtimes', `deno:${dp}`); } } catch {}
                  try {
                    require('child_process').execFileSync(PY, args, {encoding:'utf-8', timeout:90000, maxBuffer:1024*1024*8});
                    let finalCache = null;
                    if (fs.existsSync(cacheOut) && fs.statSync(cacheOut).size>1000) finalCache = cacheOut;
                    else {
                      try {
                        const files = fs.readdirSync(bgmCacheDir).filter(f=> f.endsWith('.mp3') && fs.statSync(path.join(bgmCacheDir,f)).size>1000);
                        const cand = files.find(f=> f.includes(idForCache)) || files.sort((a,b)=> fs.statSync(path.join(bgmCacheDir,b)).mtimeMs - fs.statSync(path.join(bgmCacheDir,a)).mtimeMs)[0];
                        if (cand) finalCache = path.join(bgmCacheDir, cand);
                      } catch {}
                    }
                    if (finalCache) {
                      try { fs.copyFileSync(finalCache, targetMp3); bgmPath = targetMp3; } catch {}
                      if (bgmPath) try{fs.appendFileSync(logPath, `[INFO] Backsound TikTok OK ${finalCache} ${(fs.statSync(finalCache).size/1024).toFixed(0)}KB -> ${targetMp3} (cached)\n`);}catch{}
                      else try{fs.appendFileSync(logPath, `[WARN] Backsound TikTok download ok tapi copy ke ${targetMp3} gagal\n`);}catch{}
                    } else try{fs.appendFileSync(logPath, `[WARN] Backsound TikTok download selesai tapi file tidak ditemukan di ${bgmCacheDir}\n`);}catch{}
                  } catch(e3) { try{fs.appendFileSync(logPath, `[WARN] Backsound TikTok gagal: ${String(e3.message||e3).slice(0,350)}\n`);}catch{} }
                }
              } else {
                try { fs.appendFileSync(logPath, `[INFO] Backsound download dari music_url: ${String(musicUrl).slice(0, 80)}\n`); } catch {}
                ensureTemp();
                const args = ['-m','yt_dlp','-x','--audio-format','mp3','--audio-quality','0','--no-playlist','--no-warnings','-o', targetMp3.replace(/\.mp3$/,'.%(ext)s'), String(musicUrl)];
                if (cookiesPath) { args.splice(args.length-1, 0, '--cookies', cookiesPath); }
                try { const dp = require('child_process').execFileSync(PY, ['-c','from utils.helpers import get_deno_path;import pathlib;print(get_deno_path() or "")'], {encoding:'utf-8', timeout:4000, cwd: ROOT}).trim(); if (dp && fs.existsSync(dp)) { args.splice(args.length-1, 0, '--js-runtimes', `deno:${dp}`); } } catch {}
                try {
                  require('child_process').execFileSync(PY, args, {encoding:'utf-8', timeout:60000, maxBuffer:1024*1024*4});
                  if (fs.existsSync(targetMp3) && fs.statSync(targetMp3).size>1000) { bgmPath = targetMp3; }
                  else {
                    try { const files = fs.readdirSync(path.join(sessionDir,'_temp')); const cand = files.find(f=> f.startsWith('music.') && fs.statSync(path.join(sessionDir,'_temp',f)).size>1000); if (cand) bgmPath = path.join(sessionDir,'_temp',cand); } catch {}
                  }
                  if (bgmPath) try { fs.appendFileSync(logPath, `[INFO] Backsound OK ${bgmPath} ${(fs.statSync(bgmPath).size/1024).toFixed(0)}KB\n`); } catch {}
                  else try { fs.appendFileSync(logPath, `[WARN] Backsound download selesai tapi file tidak ditemukan\n`); } catch {}
                } catch(e4) { try { fs.appendFileSync(logPath, `[WARN] Backsound gagal (music_url): ${String(e4.message||e4).slice(0, 250)}\n`); } catch {} }
              }
            } else if (musicQuery) {
              try { fs.appendFileSync(logPath, `[INFO] Backsound search query: ${String(musicQuery).slice(0, 80)}\n`); } catch {}
              ensureTemp();
              const searchUrl = `ytsearch1:${String(musicQuery).slice(0, 80)}`;
              const args2 = ['-m','yt_dlp','-x','--audio-format','mp3','--audio-quality','0','--no-playlist','--no-warnings','-o', targetMp3.replace(/\.mp3$/,'.%(ext)s'), searchUrl];
              if (cookiesPath) { args2.splice(args2.length-1, 0, '--cookies', cookiesPath); }
              try {
                require('child_process').execFileSync(PY, args2, {encoding:'utf-8', timeout:60000, maxBuffer:1024*1024*4});
                if (fs.existsSync(targetMp3) && fs.statSync(targetMp3).size>1000) bgmPath = targetMp3;
                else { try { const files = fs.readdirSync(path.join(sessionDir,'_temp')); const cand = files.find(f=> f.startsWith('music.') && fs.statSync(path.join(sessionDir,'_temp',f)).size>1000); if (cand) bgmPath = path.join(sessionDir,'_temp',cand); } catch {} }
                if (bgmPath) try { fs.appendFileSync(logPath, `[INFO] Backsound search OK ${bgmPath}\n`); } catch {}
              } catch(e5) { try { fs.appendFileSync(logPath, `[WARN] Backsound search gagal: ${String(e5.message||e5).slice(0, 250)}\n`); } catch {} }
            } else {
              try { fs.appendFileSync(logPath, `[INFO] Brief tidak minta backsound spesifik - skip download music\n`); } catch {}
            }
          } catch(e) { try { fs.appendFileSync(logPath, `[WARN] Backsound step error: ${String(e.message||e).slice(0,200)}\n`); } catch {} }

          job.stage = 'highlight';
          let sourceUrl = '';
          let _allSources = [];
          const isGDriveFolder = (u)=> /\/drive\/folders\//.test(String(u));
          const isExcludedLabel = (lb)=> /poster|thumbnail|cover|image|foto|banner|sample|mockup/i.test(String(lb||'').toLowerCase());
          const isFileUrl = (u)=> !isGDriveFolder(u);
          // helper: expand GDrive folder - PRIMARY gdown --json (rclone-friendly), fallback yt-dlp
          // gdown --json bekerja untuk folder public bahkan saat yt-dlp 400 Private/Unavailable
          const tryExpandFolder = (folderUrl)=>{
            // PRIMARY: gdown --json (rclone stack: gdown list + rclone/gdown download)
            try {
              const out = require('child_process').execFileSync(PY, ['-m','gdown','--json', String(folderUrl)], {encoding:'utf-8', timeout:25000, maxBuffer:1024*1024*8});
              const arr = JSON.parse(out);
              if (Array.isArray(arr) && arr.length) {
                const isVideoPath = (pp)=> /\.(mp4|mov|mkv|webm|avi|m4v|mpg|mpeg)$/i.test(String(pp||''));
                const isExcludedPath = (pp)=> /(^|\/)\.DS_Store$/i.test(String(pp)) || /\.(zip|rar|7z|wav|mp3|aac|flac|docx?|pdf|xlsx?|pptx?|txt|html)$/i.test(String(pp)) || isExcludedLabel(String(pp||''));
                let videoUrls = arr.filter(e=> isVideoPath(e.path) && !isExcludedPath(e.path)).map(e=> e.url).filter(Boolean);
                if (!videoUrls.length) {
                  const fallback = arr.filter(e=> !isExcludedPath(e.path) && !/\.(docx|pdf|zip|wav|html)$/i.test(String(e.path||''))).map(e=> e.url).filter(Boolean);
                  if (fallback.length) videoUrls = fallback;
                }
                // prefer mp4 > mov > mkv > webm (mp4 biasanya <500MB, mov raw besar)
                if (videoUrls.length) {
                  const urlToPath = new Map(arr.map(e=>[e.url, e.path]));
                  const scoreExt = (u, pp)=>{ const p=String(pp||'').toLowerCase(); if (p.endsWith('.mp4')) return 100; if (p.endsWith('.mov')) return 80; if (p.endsWith('.mkv')) return 70; if (p.endsWith('.webm')) return 60; if (p.endsWith('.avi')) return 55; return 50; };
                  videoUrls.sort((a,b)=> scoreExt(b, urlToPath.get(b)) - scoreExt(a, urlToPath.get(a)));
                  try { fs.appendFileSync(logPath, `[INFO] gdown expand ${String(folderUrl).slice(0,60)} -> ${arr.length} entries, video ${videoUrls.length} (pref mp4)\n`); } catch {}
                  return videoUrls;
                }
                const allUrls = arr.map(e=> e.url).filter(Boolean);
                if (allUrls.length) { try { fs.appendFileSync(logPath, `[INFO] gdown expand ${String(folderUrl).slice(0,60)} -> ${arr.length} entries (no video filter, return all)\n`); } catch {} return allUrls; }
              }
            } catch (e) {
              try { fs.appendFileSync(logPath, `[WARN] gdown expand gagal: ${String(folderUrl).slice(0,60)} -> ${String(e.message||e).slice(0,180)}\n`); } catch {}
            }
            // FALLBACK: yt-dlp --flat-playlist (legacy, sering 400 untuk private)
            try {
              const cookiesPath = (()=>{ const c1=path.join(ROOT,'cookies.txt'); if (fs.existsSync(c1)) return c1; const c2=path.join(ROOT,'output','cookies.txt'); if (fs.existsSync(c2)) return c2; return null; })();
              const args = ['-m','yt_dlp','--flat-playlist','--skip-download','-J', folderUrl];
              if (cookiesPath) { args.splice(args.length-1,0,'--cookies',cookiesPath); }
              const out = require('child_process').execFileSync(PY, args, {encoding:'utf-8', timeout:18000, maxBuffer:1024*1024*8});
              const j = JSON.parse(out);
              const entries = Array.isArray(j.entries) ? j.entries : (Array.isArray(j) ? j : []);
              const fileUrls = entries.map(e=>{
                const id = e.id || e.url || '';
                if (!id) return null;
                if (/^[\w-]{25,}/.test(String(id)) && !String(id).startsWith('http')) return `https://drive.google.com/file/d/${id}/view`;
                if (String(id).startsWith('http')) return String(id);
                return null;
              }).filter(Boolean);
              const isImageEntry = (e)=> /\.(jpg|jpeg|png|webp|gif|bmp|pdf)$/i.test(String(e.title||'')) || String(e.ext||'').match(/^(jpg|jpeg|png|webp|gif|bmp|pdf)$/i) || isExcludedLabel(String(e.title||''));
              const videoEntries = entries.filter(e=> !isImageEntry(e));
              const videoUrls = videoEntries.map(e=>{
                const id = e.id || '';
                if (/^[\w-]{25,}/.test(String(id)) && !String(id).startsWith('http')) return `https://drive.google.com/file/d/${id}/view`;
                return null;
              }).filter(Boolean);
              return videoUrls.length ? videoUrls : fileUrls;
            } catch (e) {
              try { fs.appendFileSync(logPath, `[WARN] Expand folder yt-dlp fallback gagal: ${String(folderUrl).slice(0,60)} -> ${String(e.message||e).slice(0,180)}\n`); } catch {}
              return [];
            }
          };
          const getRanked = (arr)=>{
            const norm = (arr||[]).map(x=> typeof x==='string' ? {url:x,label:''} : {url:x.url||x,label:x.label||''}).filter(o=>o.url);
            if (!norm.length) return [];
            let candidates = norm.filter(o=> !isExcludedLabel(o.label));
            if (!candidates.length) candidates = norm;
            const score = (o)=>{
              const u=o.url; const l=u.toLowerCase();
              if (isExcludedLabel(o.label)) return -50;
              if (l.includes('youtu.be')||l.includes('youtube.com')) return 100;
              if (l.includes('drive.google.com') && isFileUrl(u)) return 90;
              if (l.includes('drive.google.com') && isGDriveFolder(u)) return 75;
              if (/tiktok\.com\/@[^/]+\/video\/\d+/.test(l) || /\/video\/\d+/.test(l)) return 80;
              if (l.includes('tiktok.com')) return 20;
              if (l.includes('instagram.com')) return 70;
              if (l.includes('facebook.com')||l.includes('fb.watch')) return 70;
              if (l.startsWith('http')) return 50;
              return 0;
            };
            return candidates.map(o=> ({...o, _score: score(o)})).sort((a,b)=> b._score - a._score);
          };
          const checkTooLarge = (url, limitMB=500)=>{
            try {
              const limit = limitMB*1024*1024;
              // pakai yt-dlp --dump-json --skip-download untuk cek filesize; timeout 12s
              const PY2 = process.env.PY || PY || 'python3';
              // GDrive dan YouTube sama: coba dump-json
              const args = ['-m','yt_dlp','--dump-json','--skip-download','--no-warnings','--no-playlist', url];
              // tambah cookies jika ada
              const cookCandidates = [require('path').join(ROOT,'cookies.txt'), require('path').join(ROOT,'output','cookies.txt')];
              let cook = null; for (const c of cookCandidates) { try { if (require('fs').existsSync(c) && require('fs').statSync(c).size>0) { cook=c; break; } } catch{} }
              if (cook) args.splice(args.length-1,0,'--cookies',cook);
              // deno jika ada (untuk youtube)
              try { const _dp = require('child_process').execFileSync(PY2, ['-c','from utils.helpers import get_deno_path;import pathlib;print(get_deno_path() or "")'], {encoding:'utf-8', timeout:4000, cwd: ROOT}).trim(); if (_dp) { /* js_runtimes handled via .netrc? yt-dlp auto? skip */ } } catch {}
              const out = require('child_process').execFileSync(PY2, args, {encoding:'utf-8', timeout:12000, maxBuffer:1024*1024*4});
              const info = JSON.parse(out);
              let sz = info.filesize || info.filesize_approx || 0;
              if (!sz && Array.isArray(info.formats)) {
                try { sz = Math.max(...info.formats.map(f=> f.filesize || f.filesize_approx || 0)); } catch {}
              }
              if (sz && sz > limit) return { too:true, size: sz, pretty: (sz/1024/1024).toFixed(1)+' MB' };
            } catch(e) {
              // jika dump-json gagal (misal GDrive private), jangan blok - biarkan fallback handle, tapi coba HEAD untuk GDrive
              try {
                if (/drive\.google\.com/.test(String(url))) {
                  // HEAD via python requests cepat (tanpa yt-dlp) - optional, skip jika gagal
                }
              } catch {}
            }
            return { too:false };
          };
          try {
            const brief = JSON.parse(fs.readFileSync(path.join(sessionDir, 'campaign_brief.json'), 'utf8'));
            _allSources = (brief.source_links || []).map(s=> typeof s==='string' ? s : {url:s.url,label:s.label||''}).filter(x=> typeof x==='string' ? x : x.url);
            const ranked = getRanked(_allSources);
            if (ranked.length) {
              try { fs.appendFileSync(logPath, `[INFO] Sources ranked ${ranked.length}: ${ranked.map(r=> r.label||r.url.slice(0,40)+' score='+r._score).join(' | ').slice(0,400)}\n`); } catch {}
              // coba dari skor tertinggi: kalau folder, expand dulu; kalau file, pakai langsung (skip poster) + guard >500MB
              for (const cand of ranked) {
                const u = cand.url;
                // guard besar >500MB - diskusi dulu, cek hanya untuk GDrive (YouTube skip)
                if (/drive\.google\.com/.test(String(u))) {
                  try {
                    const chk = checkTooLarge(u, 500);
                    if (chk.too) { try { fs.appendFileSync(logPath, `[SKIP] File besar ${chk.pretty} (>500 MB) - diskusi dulu, skip: ${cand.label||''} ${u.slice(0,70)}\n`); } catch {} continue; }
                  } catch {}
                }
                if (isGDriveFolder(u)) {
                  try { fs.appendFileSync(logPath, `[INFO] Folder detected, expanding: ${cand.label||''} ${u.slice(0,70)}\n`); } catch {}
                  const expanded = tryExpandFolder(u);
                  if (expanded.length) {
                    // filter expanded: skip file besar >500MB
                    let picked = null;
                    for (const eu of expanded) {
                      if (/drive\.google\.com/.test(String(eu))) {
                        try {
                          const chk2 = checkTooLarge(eu, 500);
                          if (chk2.too) { try { fs.appendFileSync(logPath, `[SKIP] Expanded file besar ${chk2.pretty} (>500 MB) skip: ${eu.slice(0,70)}\n`); } catch {} continue; }
                        } catch {}
                      }
                      picked = eu; break;
                    }
                    if (picked) {
                      sourceUrl = picked;
                      try { fs.appendFileSync(logPath, `[INFO] Folder expanded ${expanded.length} files -> picked ${sourceUrl.slice(0,80)}\n`); } catch {}
                      break;
                    } else {
                      try { fs.appendFileSync(logPath, `[WARN] Folder ${cand.label||u.slice(0,40)} semua file >500MB/kosong/private, coba sumber berikutnya\n`); } catch {}
                      continue;
                    }
                  } else {
                    try { fs.appendFileSync(logPath, `[WARN] Folder ${cand.label||u.slice(0,40)} kosong/private, coba sumber berikutnya\n`); } catch {}
                    continue;
                  }
                } else {
                  // file/video url - skip kalau label poster dan masih ada kandidat lain
                  if (isExcludedLabel(cand.label) && ranked.length>1) continue;
                  sourceUrl = u;
                  try { fs.appendFileSync(logPath, `[INFO] Picked file: ${cand.label||''} ${u.slice(0,80)} score=${cand._score}\n`); } catch {}
                  break;
                }
              }
              // fallback jika semua folder gagal dan tidak ada file terpilih - tetap guard besar
              if (!sourceUrl && ranked.length) {
                const candsF = ranked.filter(r=> isFileUrl(r.url) && !isExcludedLabel(r.label));
                let pickedF = null;
                for (const r of (candsF.length? candsF : ranked.filter(r=> isFileUrl(r.url)))) {
                  if (/drive\.google\.com/.test(String(r.url))) {
                    try { const chkF = checkTooLarge(r.url, 500); if (chkF.too) { try { fs.appendFileSync(logPath, `[SKIP] Fallback skip besar ${chkF.pretty}: ${r.url.slice(0,60)}\n`); } catch {} continue; } } catch {}
                  }
                  pickedF = r; break;
                }
                if (pickedF) sourceUrl = pickedF.url;
                else if (ranked.length && !/drive\.google\.com/.test(String(ranked[0].url))) sourceUrl = ranked[0].url;
              }
            }
            if (!_allSources.length) sourceUrl = (brief.source_links && brief.source_links[0] && brief.source_links[0].url) || '';
            if (sourceUrl) try { fs.appendFileSync(logPath, `[INFO] Final source: ${sourceUrl.slice(0,90)}\n`); } catch {}
          } catch (e) { try { fs.appendFileSync(logPath, `[WARN] pick error: ${String(e.message||e).slice(0,200)}\n`); } catch {} }
          if (!sourceUrl && j.campaign && j.campaign.source_links) {
            const raw = j.campaign.source_links.map(s=> typeof s==='string' ? {url:s,label:''} : {url:s.url||s,label:s.label||''}).filter(o=>o.url);
            if (raw.length) {
              const isExcluded2 = (lb)=>/poster|thumbnail|cover|image|foto|banner|sample|mockup/i.test(String(lb||'').toLowerCase());
              const ranked2 = getRanked(raw);
              if (ranked2.length) {
                for (const cand of ranked2) {
                  const u = cand.url;
                  if (/drive\.google\.com/.test(String(u))) {
                    try { const chk = checkTooLarge(u, 500); if (chk.too) { try { fs.appendFileSync(logPath, `[SKIP] (campaign) File besar ${chk.pretty} skip: ${cand.label||''} ${u.slice(0,60)}\n`); } catch {} continue; } } catch {}
                  }
                  if (isGDriveFolder(u)) {
                    const expanded2 = tryExpandFolder(u);
                    if (expanded2.length) {
                      let picked2=null;
                      for (const eu2 of expanded2) {
                        if (/drive\.google\.com/.test(String(eu2))) {
                          try { const chk2=checkTooLarge(eu2,500); if (chk2.too) { try { fs.appendFileSync(logPath, `[SKIP] (campaign) Expanded besar ${chk2.pretty} skip ${eu2.slice(0,60)}\n`);} catch{} continue; } } catch{}
                        }
                        picked2=eu2; break;
                      }
                      if (picked2) { sourceUrl = picked2; break; }
                      else continue;
                    }
                    else continue;
                  } else {
                    if (isExcluded2(cand.label) && ranked2.length>1) continue;
                    sourceUrl = u; break;
                  }
                }
                if (!sourceUrl && ranked2.length) {
                  let pickedF2=null;
                  const cands2 = ranked2.filter(r=> isFileUrl(r.url) && !isExcluded2(r.label));
                  for (const r of (cands2.length? cands2 : ranked2.filter(r=> isFileUrl(r.url)))) {
                    if (/drive\.google\.com/.test(String(r.url))) {
                      try { const chkF2=checkTooLarge(r.url,500); if (chkF2.too) continue; } catch{}
                    }
                    pickedF2=r; break;
                  }
                  if (pickedF2) sourceUrl = pickedF2.url;
                  else if (ranked2.length && !/drive\.google\.com/.test(String(ranked2[0].url))) sourceUrl = ranked2[0].url;
                }
              }
            }
          }
          if (!sourceUrl) {
            try { const sd0 = JSON.parse(fs.readFileSync(path.join(sessionDir, 'session_data.json'), 'utf8')); sourceUrl = sd0.url || ''; } catch {}
          }
          if (!sourceUrl) {
            const err = 'Tidak ada source video di campaign ini (source_links kosong)';
            try { fs.appendFileSync(logPath, `\n[ERROR] ${err}\n`); } catch {}
            job.code = 1; job.stage = 'failed'; job.finishedAt = Date.now();
            try { fs.writeFileSync(resultFile, JSON.stringify({ ok: false, stage: 'highlight', error: err })); } catch {}
            return;
          }
          try {
            const sdPath = path.join(sessionDir, 'session_data.json');
            if (fs.existsSync(sdPath)) {
              let sd = JSON.parse(fs.readFileSync(sdPath, 'utf8'));
              sd.campaign = sd.campaign || {};
              sd.campaign.preset = preset;
              sd.campaign.num_clips = numClips;
              sd.preset = preset;
              fs.writeFileSync(sdPath, JSON.stringify(sd, null, 2));
            }
          } catch {}
          try { fs.appendFileSync(logPath, `\n[INFO] Source: ${sourceUrl}\n[INFO] Starting highlight num_clips=${numClips} -> ${sessionId}\n`); } catch {}
          const child2 = spawn(PY, [path.join(__dirname, 'phase1_create.py'), String(sourceUrl), String(numClips), resultFile, sessionDir], { env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
          job.proc = child2;
          const out2 = fs.createWriteStream(logPath, { flags: 'a' });
          child2.stdout.pipe(out2); child2.stderr.pipe(out2);
          child2.on('close', code2 => {
            try { out2.end(); } catch {}
            if (code2 !== 0) {
              job.code = code2; job.finishedAt = Date.now(); job.stage = 'failed';
              try { const r = JSON.parse(fs.readFileSync(resultFile, 'utf8')); job.error = r.error; } catch {}
              try { fs.appendFileSync(logPath, `\n[ERROR] Highlight failed code ${code2}\n`); } catch {}
              return;
            }
            let r2 = null;
            try { r2 = JSON.parse(fs.readFileSync(resultFile, 'utf8')); } catch { r2 = null; }
            if (!r2 || !r2.ok) {
              job.code = 1; job.stage = 'failed'; job.finishedAt = Date.now();
              try { fs.appendFileSync(logPath, `\n[ERROR] Highlight result not ok: ${JSON.stringify(r2).slice(0, 600)}\n`); } catch {}
              return;
            }
            const count = r2.count || 0;
            job.count = count;
            // --- Hashtag/hook enrichment: inject hashtags_required + suggested ke session_data highlights captions ---
            try {
              if (aiBrief && (aiBrief.hashtags_required||aiBrief.hashtags_suggested)) {
                const sdPath2 = path.join(sessionDir, 'session_data.json');
                if (fs.existsSync(sdPath2)) {
                  let sd2 = JSON.parse(fs.readFileSync(sdPath2, 'utf8'));
                  const reqTags = (aiBrief.hashtags_required||[]).join(' ');
                  const sugTags = (aiBrief.hashtags_suggested||[]).slice(0,3).join(' ');
                  const allTags = [reqTags, sugTags].filter(Boolean).join(' ').trim();
                  const hook = aiBrief.hook_wajib || '';
                  if (sd2.highlights && Array.isArray(sd2.highlights)) {
                    sd2.highlights.forEach(h=>{
                      // simpan hashtag di highlight untuk sesi/session.html
                      if (allTags) h.hashtags = allTags;
                      if (hook && h.timed_title && !String(h.timed_title).toLowerCase().includes(String(hook).toLowerCase().slice(0,12))) {
                        // jangan ubah timed_title paksa, cuma catat hook terpisah
                        h.hook_wajib = hook;
                      }
                      h.brief_hashtags_required = aiBrief.hashtags_required||[];
                      h.brief_hashtags_suggested = aiBrief.hashtags_suggested||[];
                    });
                    // also store at session top
                    sd2.ai_brief = aiBrief;
                    sd2.bgm_path = bgmPath || null;
                    fs.writeFileSync(sdPath2, JSON.stringify(sd2, null, 2));
                    try { fs.appendFileSync(logPath, `[INFO] Hashtags injected ke ${sd2.highlights.length} highlights: ${allTags.slice(0,100)}\n`); } catch {}
                  }
                }
              }
            } catch(e) { try { fs.appendFileSync(logPath, `[WARN] Hashtag inject gagal: ${String(e.message||e).slice(0,150)}\n`); } catch {} }
            if (!autoRender) {
              job.code = 0; job.finishedAt = Date.now(); job.stage = 'done';
              invalidateSessions();
              return;
            }
            job.stage = 'render';
            try { fs.appendFileSync(logPath, `\n[INFO] Highlight OK ${count} clips, auto-render top ${topN} preset=${preset}\n`); } catch {}
            let highlights = [];
            try { const sd2 = JSON.parse(fs.readFileSync(path.join(sessionDir, 'session_data.json'), 'utf8')); highlights = sd2.highlights || []; } catch {}
            highlights.sort((a, b) => (b.virality_score || 0) - (a.virality_score || 0));
            const selIdx = highlights.slice(0, topN).map((_, i) => i);
            if (!selIdx.length) {
              job.code = 0; job.stage = 'done'; job.finishedAt = Date.now();
              try { fs.writeFileSync(resultFile, JSON.stringify({ ok: true, stage: 'done', session_id: sessionId, session_dir: sessionDir, count, rendered: 0 })); } catch {}
              invalidateSessions();
              return;
            }
            const selStr = selIdx.join(',');
            const env = { ...process.env, SELECTED: selStr, ADD_HOOK: '1', ADD_CAPS: '1', PRESET: preset, PYTHONIOENCODING: 'utf-8' };
            if (bgmPath && fs.existsSync(bgmPath)) { env.BGM_PATH = bgmPath; try { fs.appendFileSync(logPath, `[INFO] Render akan pakai backsound ${bgmPath}\n`); } catch {} }
            // hashtags already injected into session_data.json (hashtags_all + brief_hashtags_*), env kept for future metadata hook
            if (aiBrief && aiBrief.hashtags_required) { try { env.HASHTAGS_REQ = (aiBrief.hashtags_required||[]).join(' '); env.HASHTAGS_SUG = (aiBrief.hashtags_suggested||[]).join(' '); } catch {} }
            const renderLog = path.join(sessionDir, 'process.log');
            try { fs.mkdirSync(path.dirname(renderLog), { recursive: true }); } catch {}
            try { fs.appendFileSync(renderLog, `\n===== auto-render start ${new Date().toISOString()} preset=${preset} selected=${selStr} =====\n`); } catch {}
            const procChild = spawn(PY, [path.join(__dirname, 'process_session.py'), sessionDir], { env });
            job.proc = procChild;
            const outRender = fs.createWriteStream(renderLog, { flags: 'a' });
            const campAppend = fs.createWriteStream(logPath, { flags: 'a' });
            procChild.stdout.on('data', d => { try { outRender.write(d); campAppend.write(d); } catch {} });
            procChild.stderr.on('data', d => { try { outRender.write(d); campAppend.write(d); } catch {} });
            procChild.on('close', code3 => {
              try { outRender.end(); campAppend.end(); } catch {}
              job.code = code3; job.finishedAt = Date.now();
              if (code3 === 0) {
                job.stage = 'done';
                try {
                  const sdFinal = JSON.parse(fs.readFileSync(path.join(sessionDir, 'session_data.json'), 'utf8'));
                  const clipsCount = (sdFinal.highlights || []).length;
                  // count actual mp4 files
                  let renderedFiles = 0;
                  try { renderedFiles = fs.readdirSync(path.join(sessionDir, 'clips')).filter(f => fs.statSync(path.join(sessionDir, 'clips', f)).isDirectory()).length; } catch {}
                  fs.writeFileSync(resultFile, JSON.stringify({ ok: true, stage: 'done', session_id: sessionId, session_dir: sessionDir, count, rendered: selIdx.length, clips: renderedFiles }));
                } catch { try { fs.writeFileSync(resultFile, JSON.stringify({ ok: true, stage: 'done', session_id: sessionId, count, rendered: selIdx.length })); } catch {} }
                invalidateSessions();
              } else {
                job.stage = 'failed';
                try { fs.writeFileSync(resultFile, JSON.stringify({ ok: false, stage: 'render', error: `Render gagal code ${code3}`, session_id: sessionId, count })); } catch {}
                try { fs.appendFileSync(logPath, `\n[ERROR] Render failed code ${code3}\n`); } catch {}
              }
            });
          });
        });
        json(res, 200, { ok: true, started: true, campId, session_id: `tk_${campId}` });
      });
      return;
    }
    // GET /api/campaign/auto/status/:id (+ alias ai-create)
    if ((p.startsWith('/api/campaign/auto/status/') || p.startsWith('/api/campaign/ai-create/status/')) && req.method === 'GET') {
      const campId = p.split('/').filter(Boolean).pop();
      const job = CAMPAIGN_JOBS.get(campId);
      if (!job) return json(res, 404, { error: 'no job', campId });
      let result = null;
      try { if (job.code !== undefined && fs.existsSync(job.resultFile)) result = JSON.parse(fs.readFileSync(job.resultFile, 'utf8')); } catch {}
      const log = job.logPath ? tailFile(job.logPath, 12000) : '';
      return json(res, 200, { campId, stage: job.stage, running: job.code === undefined, code: job.code, startedAt: job.startedAt, finishedAt: job.finishedAt || null, elapsed_s: Math.round(((job.code !== undefined && job.finishedAt ? job.finishedAt : Date.now()) - job.startedAt) / 1000), count: job.count || 0, session_id: job.session_id, log, progress: parseOverall(log), result });
    }
    // POST /api/campaign/auto/cancel/:id (+ alias ai-create)
    if ((p.startsWith('/api/campaign/auto/cancel/') || p.startsWith('/api/campaign/ai-create/cancel/')) && req.method === 'POST') {
      const campId = p.split('/').filter(Boolean).pop();
      const job = CAMPAIGN_JOBS.get(campId);
      if (!job || job.code !== undefined) return json(res, 404, { error: 'Tidak ada job berjalan', campId });
      try { if (job.proc && !job.proc.killed) { try { process.kill(-job.proc.pid, 'SIGKILL'); } catch { try { job.proc.kill('SIGKILL'); } catch {} } } } catch {}
      job.code = 130; job.finishedAt = Date.now(); job.stage = 'cancelled';
      try { fs.appendFileSync(job.logPath, '\n[CANCELLED by user]\n'); } catch {}
      return json(res, 200, { ok: true, cancelled: true, campId });
    }

    // GET/POST /api/sessions/:session/subtitle - ambil & simpan editan subtitle SRT
    const mSub = p.match(/^\/api\/sessions\/([^/]+)\/subtitle$/);
    if (mSub) {
      const dir = path.join(SESSIONS, safe(mSub[1]));
      if (!dir.startsWith(SESSIONS) || !fs.existsSync(dir)) return json(res, 404, { error: 'session not found' });
      let srt = fs.readdirSync(dir).find(f => f.endsWith('.srt'));
      if (req.method === 'GET') {
        if (!srt) return json(res, 404, { error: 'no srt' });
        res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
        return res.end(fs.readFileSync(path.join(dir, srt)));
      }
      if (req.method === 'POST') {
        let body = '';
        req.on('data', c => body += c);
        req.on('end', () => {
          let o = {};
          try { o = JSON.parse(body || '{}'); } catch {}
          if (typeof o.content !== 'string') return json(res, 400, { error: 'content required' });
          if (!srt) srt = 'transcript.srt';
          const target = path.join(dir, srt);
          fs.writeFileSync(target, o.content, 'utf8');
          return json(res, 200, { ok: true });
        });
        return;
      }
    }
    // GET/POST /api/presets - simpan & ambil custom presets
    if (p === '/api/presets') {
      const pFile = path.join(ROOT, 'config', 'custom_presets.json');
      if (req.method === 'GET') {
        try {
          if (!fs.existsSync(pFile)) return json(res, 200, {});
          return json(res, 200, JSON.parse(fs.readFileSync(pFile, 'utf8')));
        } catch { return json(res, 200, {}); }
      }
      if (req.method === 'POST') {
        let body = '';
        req.on('data', c => body += c);
        req.on('end', () => {
          let o = {};
          try { o = JSON.parse(body || '{}'); } catch {}
          if (!o.name || typeof o.name !== 'string' || (!o.cfg && !o.config)) return json(res, 400, { error: 'invalid preset format' });
          const presetCfg = o.cfg || o.config;
          try {
            fs.mkdirSync(path.dirname(pFile), { recursive: true });
            let current = {};
            try { if (fs.existsSync(pFile)) current = JSON.parse(fs.readFileSync(pFile, 'utf8')); } catch {}
            if (o.delete) {
              delete current[o.name];
            } else {
              current[o.name] = { label: o.label || o.name, desc: o.desc || 'Custom preset', cfg: presetCfg, config: presetCfg };
            }
            fs.writeFileSync(pFile, JSON.stringify(current, null, 2), 'utf8');
            return json(res, 200, { ok: true, presets: current });
          } catch (e) { return json(res, 500, { error: String(e) }); }
        });
        return;
      }
    }
    // POST /api/delete/:session/:clipDir - hapus folder klip (trash bila ada, fallback rm)
    const mDel = p.match(/^\/api\/delete\/([^/]+)\/([^/]+)$/);
    if (mDel && req.method === 'POST') {
      const dir = path.join(SESSIONS, safe(mDel[1]), 'clips', safe(mDel[2]));
      if (!dir.startsWith(SESSIONS) || !fs.existsSync(dir)) return json(res, 404, { error: 'not found' });
      invalidateSessions();
            // Cross-platform delete: shell-trash (Win/mac/Linux) bila ada, fallback rm
            try {
              require('shell-trash').trash(dir).then(
                () => json(res, 200, { ok: true, method: 'trash' }),
                () => { fs.rmSync(dir, { recursive: true, force: true }); json(res, 200, { ok: true, method: 'rm' }); }
              );
            } catch { fs.rmSync(dir, { recursive: true, force: true }); json(res, 200, { ok: true, method: 'rm' }); }
            return;
    }
    // POST /api/delete-session/:session - hapus seluruh folder sesi (trash bila ada, fallback rm)
    const mDelS = p.match(/^\/api\/delete-session\/([^/]+)$/);
    if (mDelS && req.method === 'POST') {
      const dir = path.join(SESSIONS, safe(mDelS[1]));
      if (!dir.startsWith(SESSIONS) || !fs.existsSync(dir)) return json(res, 404, { error: 'not found' });
      invalidateSessions();
            // Cross-platform delete: shell-trash (Win/mac/Linux) bila ada, fallback rm
            try {
              require('shell-trash').trash(dir).then(
                () => json(res, 200, { ok: true, method: 'trash' }),
                () => { fs.rmSync(dir, { recursive: true, force: true }); json(res, 200, { ok: true, method: 'rm' }); }
              );
            } catch { fs.rmSync(dir, { recursive: true, force: true }); json(res, 200, { ok: true, method: 'rm' }); }
            return;
    }
    // GET /api/config - konfigurasi aktif (satu sumber dengan bot /config)
    if (p === '/api/config' && req.method === 'GET') {
      try {
        const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json')));
        const mp = cfg.mediapipe_settings || {};
        const ap = cfg.ai_providers || {};
        const fwSize = ((ap.caption_maker || {}).faster_whisper || {}).model_size || 'small';
        let fwInstalled = false;
        try {
          fwInstalled = fs.existsSync(path.join(os.homedir(), '.cache', 'huggingface', 'hub', 'models--Systran--faster-whisper-' + fwSize));
        } catch {}
        return json(res, 200, {
          hook: cfg.hook_enabled !== false,
          captions: cfg.subtitle_enabled !== false,
          watermark: !!(cfg.watermark && cfg.watermark.enabled),
          credit: !!(cfg.credit_watermark && cfg.credit_watermark.enabled),
          num_clips: cfg.num_clips ?? 5,
          resolution: cfg.resolution || '1080p',
          aspect_ratio: cfg.aspect_ratio || '9:16',
          subtitle_style: cfg.subtitle_style || 'pop',
          sync_offset: cfg.subtitle_sync_offset ?? -0.3,
          portrait_mode: cfg.portrait_mode || 'crop',
          face_tracking_mode: cfg.face_tracking_mode || 'opencv',
          smooth_follow: !!mp.smooth_follow,
          pan_speed_limit: mp.pan_speed_limit ?? 1.8,
          center_weight: mp.center_weight ?? 0.15,
          switch_threshold: mp.switch_threshold ?? 0.18,
          min_shot_duration: mp.min_shot_duration ?? 45,
          lip_activity: mp.lip_activity_threshold ?? 0.08,
          gpu: !!(cfg.gpu_acceleration && cfg.gpu_acceleration.enabled),
          hf_model: (ap.highlight_finder || {}).model || 'AUTO',
          server_url: (ap.highlight_finder || {}).base_url || '',
          fw_model: fwSize,
          fw_installed: fwInstalled,
          wm: cfg.watermark || {},
          cw: cfg.credit_watermark || {},
          hook_style: cfg.hook_style || {},
          core_model: cfg.model || 'gpt-4.1',
          tts_model: (cfg.ai_providers&&cfg.ai_providers.hook_maker&&cfg.ai_providers.hook_maker.model) || cfg.tts_model || 'tts-1',
          temperature: cfg.temperature ?? 1.0,
          subtitle_language: cfg.subtitle_language || 'id',
          hf_system_message: ((ap.highlight_finder || {}).system_message) || '',
          hf_api_key: (ap.highlight_finder || {}).api_key || cfg.api_key || process.env.HF_API_KEY || process.env.OPENAI_API_KEY || '',
          hf_api_key_set: !!((ap.highlight_finder || {}).api_key || cfg.api_key || process.env.HF_API_KEY || process.env.OPENAI_API_KEY),
          // Pro video editing features
          pro_settings: cfg.pro_settings || {},
          face_detector_model: 'mediapipe',
          font_preset: cfg.font_preset || 'DEFAULT',
          auto_broll: cfg.auto_broll || {},
          pexels_api_key: (cfg.pexels_api_key || ''),
          thumbnail: cfg.thumbnail || {},
        });
      } catch { return json(res, 500, { error: 'config.json tidak terbaca' }); }
    }
    // POST /api/config - simpan perubahan parameter (merge; key lain tidak disentuh)
    if (p === '/api/config' && req.method === 'POST') {
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        let o = {};
        try { o = JSON.parse(body || '{}'); } catch {}
        const fp = path.join(ROOT, 'config.json');
        let cfg;
        try { cfg = JSON.parse(fs.readFileSync(fp, 'utf8')); } catch { return json(res, 500, { error: 'config.json tidak terbaca' }); }
        const isNum = v => typeof v === 'number' && isFinite(v);
        if ('hook' in o) cfg.hook_enabled = !!o.hook;
        if ('captions' in o) cfg.subtitle_enabled = !!o.captions;
        if ('watermark' in o) (cfg.watermark = cfg.watermark || {}).enabled = !!o.watermark;
        if ('credit' in o) (cfg.credit_watermark = cfg.credit_watermark || {}).enabled = !!o.credit;
        if ('gpu' in o) (cfg.gpu_acceleration = cfg.gpu_acceleration || {}).enabled = !!o.gpu;
        if (isNum(o.num_clips)) cfg.num_clips = Math.max(1, Math.round(o.num_clips));
        if (typeof o.resolution === 'string' && o.resolution) cfg.resolution = o.resolution;
        if (typeof o.aspect_ratio === 'string' && o.aspect_ratio) cfg.aspect_ratio = o.aspect_ratio;
        if (typeof o.subtitle_style === 'string' && o.subtitle_style) {
          cfg.subtitle_style = o.subtitle_style === 'karaoke' ? 'karaoke' : 'pop';
        }
        if (isNum(o.sync_offset)) cfg.subtitle_sync_offset = o.sync_offset;
        if (typeof o.portrait_mode === 'string' && o.portrait_mode) cfg.portrait_mode = o.portrait_mode;
        if (typeof o.face_tracking_mode === 'string' && o.face_tracking_mode) cfg.face_tracking_mode = o.face_tracking_mode;
        cfg.mediapipe_settings = cfg.mediapipe_settings || {};
        if ('smooth_follow' in o) cfg.mediapipe_settings.smooth_follow = !!o.smooth_follow;
        for (const k of ['pan_speed_limit', 'center_weight', 'switch_threshold']) if (isNum(o[k])) cfg.mediapipe_settings[k] = o[k];
        if (isNum(o.min_shot_duration)) cfg.mediapipe_settings.min_shot_duration = Math.max(1, Math.round(o.min_shot_duration));
        if (isNum(o.lip_activity)) cfg.mediapipe_settings.lip_activity_threshold = o.lip_activity;
        // Pro video editing features
        cfg.pro_settings = cfg.pro_settings || {};
        if ('stabilize' in o) cfg.pro_settings.stabilize = !!o.stabilize;
        if (typeof o.color_grade === 'string') cfg.pro_settings.color_grade = o.color_grade;
        if (isNum(o.motion_blur)) cfg.pro_settings.motion_blur = Math.max(0, Math.min(10, o.motion_blur));
        if (isNum(o.vignette)) cfg.pro_settings.vignette = Math.max(0, Math.min(1, o.vignette));
        if (isNum(o.speed_ramp_start)) cfg.pro_settings.speed_ramp_start = Math.max(0, o.speed_ramp_start);
        if (isNum(o.speed_ramp_end)) cfg.pro_settings.speed_ramp_end = Math.max(0, o.speed_ramp_end);
        if (isNum(o.speed_factor)) cfg.pro_settings.speed_factor = Math.max(0.1, Math.min(2, o.speed_factor));
        if (isNum(o.ducking_level_db)) cfg.pro_settings.ducking_level_db = Math.max(-30, Math.min(0, o.ducking_level_db));
        cfg.face_detector_model = 'mediapipe';
        delete cfg.yolo_size;
        if (typeof o.font_preset === 'string' && o.font_preset.trim()) cfg.font_preset = o.font_preset.trim();
        if (typeof o.wm === 'object' && o.wm) {
          if (typeof o.wm.position === 'string') cfg.watermark.position = o.wm.position;
          if (typeof o.wm.text === 'string') cfg.watermark.text = o.wm.text;
          if (isNum(o.wm.padding)) cfg.watermark.padding = o.wm.padding;
        }
        if (o.thumbnail && typeof o.thumbnail === 'object') {
          cfg.thumbnail = Object.assign({}, cfg.thumbnail, o.thumbnail);
          if ('enabled' in o.thumbnail) cfg.thumbnail.enabled = !!o.thumbnail.enabled;
        }
        // Normalisasi input UI (boolean/string sederhana) ke bentuk object dict yang dipakai engine
        cfg.auto_broll = cfg.auto_broll || {};
        if (typeof o.auto_broll === 'boolean') cfg.auto_broll.enabled = o.auto_broll;
        if (typeof o.auto_broll === 'string') cfg.auto_broll.enabled = o.auto_broll !== 'none' && o.auto_broll !== 'false' && o.auto_broll !== '';
        if (typeof o.pexels_api_key === 'string') {
          const pk = o.pexels_api_key.trim();
          if (pk) cfg.pexels_api_key = pk; else delete cfg.pexels_api_key;
        }
        cfg.thumbnail = cfg.thumbnail || {};
        if (typeof o.thumbnail === 'boolean') cfg.thumbnail.enabled = o.thumbnail;
        cfg.font_preset = cfg.font_preset || 'default';
        if (typeof o.font_preset === 'string' && o.font_preset.trim()) cfg.font_preset = o.font_preset.trim();
        if (o.metadata_settings && typeof o.metadata_settings === 'object') {
          cfg.metadata_settings = Object.assign({}, cfg.metadata_settings, o.metadata_settings);
        }
        if (o.facebook_uploader && typeof o.facebook_uploader === 'object') {
          cfg.facebook_uploader = Object.assign({}, cfg.facebook_uploader, o.facebook_uploader);
          // jangan simpan field kosong (biar loader pakai env fallback)
          for (const k of ['page_id', 'access_token', 'graph_version']) if (cfg.facebook_uploader[k] === '') delete cfg.facebook_uploader[k];
        }
        cfg.ai_providers = cfg.ai_providers || {};
        cfg.ai_providers.highlight_finder = cfg.ai_providers.highlight_finder || {};
        if (typeof o.hf_model === 'string' && o.hf_model.trim()) cfg.ai_providers.highlight_finder.model = o.hf_model.trim();
        if (typeof o.server_url === 'string' && o.server_url.trim()) cfg.ai_providers.highlight_finder.base_url = o.server_url.trim();
        const fwVal = (o.fw_model || o.faster_whisper_model || '').trim();
        if (fwVal) {
          cfg.ai_providers.caption_maker = cfg.ai_providers.caption_maker || {};
          cfg.ai_providers.caption_maker.faster_whisper = Object.assign({}, cfg.ai_providers.caption_maker.faster_whisper, { model_size: fwVal });
        }
        if (o.wm && typeof o.wm === 'object') {
          cfg.watermark = cfg.watermark || {};
          if ('enabled' in o.wm) cfg.watermark.enabled = !!o.wm.enabled;
          for (const k of ['position_x', 'position_y', 'opacity', 'scale']) if (isNum(o.wm[k])) cfg.watermark[k] = o.wm[k];
        }
        if (o.cw && typeof o.cw === 'object') {
          cfg.credit_watermark = cfg.credit_watermark || {};
          if ('enabled' in o.cw) cfg.credit_watermark.enabled = !!o.cw.enabled;
          for (const k of ['position_x', 'position_y', 'size', 'opacity']) if (isNum(o.cw[k])) cfg.credit_watermark[k] = o.cw[k];
        }
        if (o.hook_style && typeof o.hook_style === 'object') {
          cfg.hook_style = Object.assign({}, cfg.hook_style);
          if (typeof o.hook_style.box_mode === 'string') cfg.hook_style.box_mode = o.hook_style.box_mode;
          if (isNum(o.hook_style.bg_opacity)) cfg.hook_style.bg_opacity = Math.max(0, Math.min(100, Math.round(o.hook_style.bg_opacity)));
          for (const k of ['font_size', 'corner_radius', 'position_x', 'position_y', 'duration']) if (isNum(o.hook_style[k])) cfg.hook_style[k] = o.hook_style[k];
          for (const k of ['font_color', 'bg_color']) if (typeof o.hook_style[k] === 'string' && /^#[0-9a-fA-F]{6}$/.test(o.hook_style[k])) cfg.hook_style[k] = o.hook_style[k];
          if ('glitch' in o.hook_style) cfg.hook_style.glitch = !!o.hook_style.glitch;
        }
        if (isNum(o.temperature)) cfg.temperature = Math.min(2, Math.max(0, o.temperature));
        if (typeof o.core_model === 'string' && o.core_model.trim()) cfg.model = o.core_model.trim();
        if (typeof o.tts_model === 'string' && o.tts_model.trim()) {
          const v=o.tts_model.trim();
          cfg.tts_model = v;
          // sync ke hook_maker juga biar proses clip pakai model terbaru
          cfg.ai_providers = cfg.ai_providers||{};
          cfg.ai_providers.hook_maker = cfg.ai_providers.hook_maker||{};
          cfg.ai_providers.hook_maker.model = v;
        }
        if (typeof o.subtitle_language === 'string' && o.subtitle_language.trim()) cfg.subtitle_language = o.subtitle_language.trim();
        cfg.ai_providers.highlight_finder = cfg.ai_providers.highlight_finder || {};
        if (typeof o.hf_system_message === 'string' && o.hf_system_message.trim()) cfg.ai_providers.highlight_finder.system_message = o.hf_system_message;
        if (typeof o.hf_api_key === 'string' && o.hf_api_key.trim()) cfg.ai_providers.highlight_finder.api_key = o.hf_api_key.trim();
        try {
          const tmp = fp + '.tmp';
          fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2));
          fs.renameSync(tmp, fp);
          json(res, 200, { ok: true });
        } catch (e) { json(res, 500, { error: String(e) }); }
      });
      return;
    }
    // POST /api/test-connection - cek reachable + valid API key (list models, OpenAI-compatible)
const TC_SRC = `import os, json
from openai import OpenAI
u=os.environ.get('TC_URL','').strip()
k=os.environ.get('TC_KEY','').strip()
try:
    c=OpenAI(api_key=k or 'x', base_url=u or None)
    m=c.models.list()
    ids=[getattr(x,'id',str(x)) for x in m.data if getattr(x,'id',str(x))]
    print(json.dumps({'ok':True,'count':len(ids),'sample':ids,'models':ids}))
except Exception as e:
    print(json.dumps({'ok':False,'error':str(e)[:300]}))`;
    // proxy TTS models via 9Router cookie auth (POST /api/auth/login {password} -> GET /api/providers)
    if (p === '/api/tts/9router' && req.method === 'POST') {
      let body=''; req.on('data',c=>body+=c); req.on('end',()=>{
        let o={}; try{o=JSON.parse(body||'{}')}catch{}
        const pwd=String(o.password||'123456').trim();
        const serverUrl=String(o.server_url||'').trim().replace(/\/v1\/?$/,'').replace(/\/$/,'') || 'http://localhost:20128';
        const http = require('http'); const https=require('https');
        const loginUrl=new URL('/api/auth/login', serverUrl+'/');
        const postData=JSON.stringify({password: pwd});
        const mod = loginUrl.protocol==='https:'?https:http;
        const reqLogin=mod.request({hostname: loginUrl.hostname, port: loginUrl.port||(loginUrl.protocol==='https:'?443:80), path: loginUrl.pathname, method:'POST', headers:{'Content-Type':'application/json','Content-Length': Buffer.byteLength(postData)}}, rLogin=>{
          let d=''; rLogin.on('data',c=>d+=c); rLogin.on('end',()=>{
            const cookies=(rLogin.headers['set-cookie']||[]).join('; ');
            if(rLogin.statusCode!==200) return json(res,200,{ok:false, error:'Login gagal: '+d.slice(0,200)});
            const provUrl=new URL('/api/providers', serverUrl+'/');
            const mod2 = provUrl.protocol==='https:'?https:http;
            const req2=mod2.request({hostname: provUrl.hostname, port: provUrl.port||(provUrl.protocol==='https:'?443:80), path: provUrl.pathname, method:'GET', headers:{'Cookie': cookies, 'Accept':'application/json'}}, r2=>{
              let dd=''; r2.on('data',c=>dd+=c); r2.on('end',()=>{
                try{
                  const j=JSON.parse(dd);
                  // extract TTS models dari connections keys modelLock_*tts*
                  // dashboard: Edge TTS, Google TTS, Local Device selalu Ready; OpenRouter/NVIDIA/ElevenLabs = Connected
                  const dashboardReady = ['edge-tts','google-tts','local-device','elevenlabs','openai','openrouter','nvidia','gemini','antigravity'];
                  const activeProviders = new Set([...(j.connections||[]).filter(c=>c.testStatus==='active').map(c=>c.provider), ...dashboardReady]);
                  const ids=[];
                  const conns = j.connections ? (Array.isArray(j.connections) ? j.connections : [j.connections]) : [];
                  conns.forEach(c=>{
                    if(!activeProviders.has(c.provider)) return;
                    Object.keys(c).forEach(k=>{
                      if(!k.startsWith('modelLock_')) return;
                      const raw=k.slice(10); // setelah modelLock_
                      // format: provider/model/voice atau model/voice
                      const full = raw.includes('/') ? (raw.startsWith(c.provider+'/')? raw : c.provider+'/'+raw) : raw;
                      if(/tts/i.test(full)) ids.push(full);
                    });
                  });
                  // fallback: cari di all keys jika masih kosong
                  if(!ids.length && j.connections && typeof j.connections==='object' && !Array.isArray(j.connections)){
                    Object.keys(j.connections).forEach(k=>{
                      const m=k.match(/^modelLock_(.+?)\//);
                      if(m && /tts/i.test(m[1])) ids.push(m[1]);
                    });
                  }
                  // tambah model TTS statis untuk provider Ready yang tidak ada di modelLock
                  const staticTts = [];
                  if(activeProviders.has('edge-tts')) staticTts.push('edge-tts/id-ID-GadisNeural','edge-tts/id-ID-ArdiNeural','edge-tts/en-US-AriaNeural','edge-tts/en-US-GuyNeural');
                  if(activeProviders.has('google-tts')) staticTts.push('google-tts/id','google-tts/en');
                  if(activeProviders.has('local-device')) staticTts.push('local-device/tts-1');
                  if(activeProviders.has('elevenlabs') || activeProviders.has('elevenlabs')) staticTts.push('elevenlabs/eleven_multilingual_v2','elevenlabs/eleven_flash_v2_5');
                  const all=[...ids, ...staticTts];
                  const uniq=[...new Set(all)];
                  if(uniq.length) return json(res,200,{ok:true, count:uniq.length, sample:uniq, activeProviders:[...activeProviders]});
                }catch(e){ return json(res,200,{ok:false, error:'Parse error:'+String(e)}); }
                return json(res,200,{ok:false, error:'Tidak ada TTS model ditemukan. Providers aktif:'+[...activeProviders].join(',')});
              });
            });
            req2.on('error',e=> json(res,200,{ok:false, error:String(e)})); req2.end();
          });
        });
        reqLogin.on('error',e=> json(res,200,{ok:false, error:String(e)})); reqLogin.write(postData); reqLogin.end();
      });
      return;
    }
    // POST /api/tts/test {server_url, api_key, model} -> test audio/speech
    if (p === '/api/tts/test' && req.method === 'POST') {
      let body=''; req.on('data',c=>body+=c); req.on('end',()=>{
        let o={}; try{o=JSON.parse(body||'{}')}catch{}
        const serverUrl=String(o.server_url||'http://localhost:20128').trim().replace(/\/$/,'');
        let apiKey=String(o.api_key||o.hf_api_key||'').trim();
        // TTS Edge butuh key eleven/sk-624..., fb-shared tidak bisa -> fallback ke hook_maker key di config
        if(!apiKey || apiKey==='fb-shared-040826'){
          try{ const cfg=JSON.parse(require('fs').readFileSync(require('path').join(__dirname,'..','config.json'),'utf8')); apiKey=(cfg.ai_providers&&cfg.ai_providers.hook_maker&&cfg.ai_providers.hook_maker.api_key)||'sk-624626b7f6d25002-7pluyc-35e01fba'; }catch{ apiKey='sk-624626b7f6d25002-7pluyc-35e01fba'; }
        }
        const model=String(o.model||'gemini/gemini-2.5-flash-preview-tts/Erinome').trim();
        const input=String(o.input||'Hello, this is a text to speech test.').slice(0,500);
        const language=String(o.language||'Indonesian').trim();
        const payload=JSON.stringify({model, input, language});
        const u=new URL('/v1/audio/speech', serverUrl+'/');
        const mod=u.protocol==='https:'?require('https'):require('http');
        const req2=mod.request({hostname:u.hostname, port:u.port||(u.protocol==='https:'?443:80), path:u.pathname, method:'POST', headers:{'Content-Type':'application/json','Authorization':`Bearer ${apiKey}`,'Content-Length': Buffer.byteLength(payload)}}, r2=>{
          let chunks=[]; r2.on('data',c=>chunks.push(c)); r2.on('end',()=>{
            const buf=Buffer.concat(chunks);
            const ct=r2.headers['content-type']||'';
            if(r2.statusCode===200 && ct.includes('audio')) return json(res,200,{ok:true, bytes:buf.length, content_type:ct});
            return json(res,200,{ok:false, error: buf.toString('utf8').slice(0,400), status:r2.statusCode});
          });
        });
        req2.on('error',e=> json(res,200,{ok:false, error:String(e)})); req2.write(payload); req2.end();
      });
      return;
    }
    // GET /api/tts/preview?model=... -> proxy audio mp3 untuk preview di browser
    if (p.startsWith('/api/tts/preview')) {
      const model = u.searchParams.get('model') || 'edge-tts/id-ID-GadisNeural';
      const apiKey = (()=>{ try{ return JSON.parse(require('fs').readFileSync(require('path').join(ROOT,'config.json'),'utf8')).ai_providers.hook_maker.api_key; }catch{ return 'sk-624626b7f6d25002-7pluyc-35e01fba'; } })();
      const serverUrl = (()=>{ try{ return JSON.parse(require('fs').readFileSync(require('path').join(ROOT,'config.json'),'utf8')).ai_providers.hook_maker.base_url || 'http://localhost:20128/v1'; }catch{ return 'http://localhost:20128/v1'; } })().replace(/\/$/,'');
      const input=(new URL(req.url,'http://x').searchParams.get('input')||'Hello, this is a text to speech test.').slice(0,500);
      const language=new URL(req.url,'http://x').searchParams.get('language')||'Indonesian';
      const payload=JSON.stringify({model, input, language});
      const uu=new URL('/v1/audio/speech', serverUrl+'/');
      const mod=uu.protocol==='https:'?require('https'):require('http');
      const req2=mod.request({hostname:uu.hostname, port:uu.port||(uu.protocol==='https:'?443:80), path:uu.pathname, method:'POST', headers:{'Content-Type':'application/json','Authorization':`Bearer ${apiKey}`,'Content-Length': Buffer.byteLength(payload)}}, r2=>{
        if(r2.statusCode===200 && (r2.headers['content-type']||'').includes('audio')){
          res.writeHead(200, {'Content-Type': r2.headers['content-type']});
          r2.pipe(res);
        } else {
          let b=''; r2.on('data',c=>b+=c); r2.on('end',()=> json(res, r2.statusCode, {error: b.slice(0,300)}));
        }
      });
      req2.on('error',e=> json(res,500,{error:String(e)})); req2.write(payload); req2.end();
      return;
    }
    if ((p === '/api/test-connection' || p === '/api/test-llm') && req.method === 'POST') {
  let body = '';
  req.on('data', c => body += c);
  req.on('end', () => {
    let o = {};
    try { o = JSON.parse(body || '{}'); } catch {}
    // Fallback ke config tersimpan kalau field kosong (supaya tidak 401 setelah reload)
    let savedUrl = '', savedKey = '';
    try {
      const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json'), 'utf8'));
      const hf = cfg.ai_providers && cfg.ai_providers.highlight_finder;
      if (hf) { savedUrl = hf.base_url || ''; savedKey = hf.api_key || ''; }
    } catch {}
    const env = { ...process.env, TC_URL: String(o.server_url || savedUrl || '').trim(), TC_KEY: String(o.api_key || o.hf_api_key || savedKey || '') };
    execFile(PY, ['-c', TC_SRC], { env }, (err, stdout, stderr) => {
      const out = (stdout || '').toString().trim().split('\n').pop();
      try { return json(res, 200, JSON.parse(out)); } catch { return json(res, 200, { ok: false, error: (stderr || stdout || '').toString().slice(-300) }); }
    });
  });
  return;
}
// GET /api/whisper/models - cek semua faster-whisper model (installed/ size)
    if (p === '/api/whisper/models') {
      const sizes = ['tiny','base','small','medium','large-v3'];
      const PYCHK = `from pathlib import Path;import sys;sys.path.insert(0,r'${ROOT.replace(/\\/g,'\\\\')}');from utils.dependency_manager import check_dependency;import json;app=Path(r'${ROOT.replace(/\\/g,'\\\\')}');print(json.dumps({s: check_dependency(f'faster_whisper_model_'+s, app) for s in ['tiny','base','small','medium','large-v3']}))`;
      execFile(PY, ['-c', PYCHK], (err, stdout) => {
        let map={}; try{ map=JSON.parse(stdout.trim().split('\n').pop()); }catch{}
        const out=sizes.map(s=>{
          const dir=path.join(ROOT,'faster_whisper_models',s);
          let bytes=0; try{ bytes=fs.statSync(path.join(dir,'model.bin')).size; }catch{}
          return { size:s, installed:!!map[s], bytes, mb: bytes?(bytes/1048576).toFixed(1)+' MB':'' };
        });
        json(res, 200, out);
      });
      return;
    }
    // POST /api/whisper/download {size} - download model async
    if (p === '/api/whisper/download' && req.method === 'POST') {
      let body=''; req.on('data',c=>body+=c); req.on('end',()=>{
        let o={}; try{o=JSON.parse(body||'{}')}catch{}
        const size=String(o.size||'').trim();
        if(!['tiny','base','small','medium','large-v3'].includes(size)) return json(res,400,{error:'invalid size'});
        const logPath=path.join(ROOT,'output',`whisper_download_${size}.log`);
        const out=fs.createWriteStream(logPath,{flags:'a'});
        fs.appendFileSync(logPath,`\n===== download ${size} ${new Date().toISOString()} =====\n`);
        const child=spawn(PY, ['-c', `import sys;sys.path.insert(0, r'${ROOT.replace(/\\/g,'\\\\')}');from pathlib import Path;from utils.dependency_manager import setup_faster_whisper_model;ok=setup_faster_whisper_model(Path(r'${ROOT.replace(/\\/g,'\\\\')}'), '${size}');print('DONE:'+str(ok));sys.exit(0 if ok else 1)`], {cwd: ROOT, env:{...process.env, PYTHONIOENCODING:'utf-8'}});
        child.stdout.pipe(out); child.stderr.pipe(out);
        child.on('close',code=>{ out.end(); });
        json(res,200,{ok:true, started:true, log: logPath});
      });
      return;
    }
    // POST /api/dependencies/install - unified download/update for binaries & pip packages
    // body: {target: 'ffmpeg'|'deno'|'mediapipe'|'yt-dlp'|'pip:<pkg>'|'whisper:tiny'|'whisper:base'|'whisper:small'|'whisper:medium'|'whisper:large-v3'}
    if (p === '/api/dependencies/install' && req.method === 'POST') {
      let body=''; req.on('data',c=>body+=c); req.on('end',()=>{
        let o={}; try{o=JSON.parse(body||'{}')}catch{}
        let target=String(o.target||'').trim();
        // normalize legacy: whisper small -> whisper:small
        if (['tiny','base','small','medium','large-v3'].includes(target)) target='whisper:'+target;
        const allowed = new Set(['ffmpeg','deno','mediapipe','yt-dlp','whisper:tiny','whisper:base','whisper:small','whisper:medium','whisper:large-v3']);
        const isPip = target.startsWith('pip:');
        const pipPkg = isPip ? target.slice(4).trim() : '';
        const isPipValid = isPip && /^[a-zA-Z0-9_\-.]+$/.test(pipPkg);
        if (!allowed.has(target) && !isPipValid) return json(res,400,{error:'invalid target: '+target});
        const jobKey = target;
        const prev = DEP_JOBS.get(jobKey);
        if (prev && prev.proc && !prev.proc.killed && prev.code===undefined) return json(res,409,{error:'masih berjalan', target});
        const safeTarget = target.replace(/[:\/]/g,'_');
        const logPath = path.join(ROOT,'output',`dep_${safeTarget}.log`);
        try{ fs.mkdirSync(path.join(ROOT,'output'),{recursive:true}); }catch{}
        fs.appendFileSync(logPath,`\n===== ${target} ${new Date().toISOString()} =====\n`);
        const out = fs.createWriteStream(logPath,{flags:'a'});
        let child;
        const env = {...process.env, PYTHONIOENCODING:'utf-8'};
        // helper to add sys.path for dependency_manager imports
        const pyPrefix = `import sys;sys.path.insert(0, r'${ROOT.replace(/\\/g,'\\\\')}');`;
        if (target==='ffmpeg') {
          child=spawn(PY, ['-c', `${pyPrefix}from pathlib import Path;from utils.dependency_manager import setup_ffmpeg;ok=setup_ffmpeg(Path(r'${ROOT.replace(/\\/g,'\\\\')}'));print('DONE:'+str(ok));sys.exit(0 if ok else 1)`], {cwd: ROOT, env});
        } else if (target==='deno') {
          child=spawn(PY, ['-c', `${pyPrefix}from pathlib import Path;from utils.dependency_manager import setup_deno;ok=setup_deno(Path(r'${ROOT.replace(/\\/g,'\\\\')}'));print('DONE:'+str(ok));sys.exit(0 if ok else 1)`], {cwd: ROOT, env});
        } else if (target==='mediapipe') {
          child=spawn(PY, ['-c', `${pyPrefix}from pathlib import Path;from utils.dependency_manager import setup_mediapipe_model;ok=setup_mediapipe_model(Path(r'${ROOT.replace(/\\/g,'\\\\')}'));print('DONE:'+str(ok));sys.exit(0 if ok else 1)`], {cwd: ROOT, env});
        } else if (target==='yt-dlp') {
          child=spawn(PY, ['-m','pip','install','--upgrade','yt-dlp'], {cwd: ROOT, env});
        } else if (target.startsWith('whisper:')) {
          const size=target.split(':')[1];
          // for update: remove old dir first if exists (force re-download)
          child=spawn(PY, ['-c', `${pyPrefix}from pathlib import Path, shutil;from utils.dependency_manager import setup_faster_whisper_model, get_faster_whisper_model_dir;app=Path(r'${ROOT.replace(/\\/g,'\\\\')}');d=get_faster_whisper_model_dir(app,'${size}');\nif d.exists() and (d/'model.bin').exists():\n  import time; print('existing '+str(d)+' will be refreshed');\nok=setup_faster_whisper_model(app,'${size}');print('DONE:'+str(ok));sys.exit(0 if ok else 1)`.replace('${size}', size)], {cwd: ROOT, env});
          // fix spawn arg interpolation - need size variable
        } else if (isPip) {
          child=spawn(PY, ['-m','pip','install','--upgrade', pipPkg], {cwd: ROOT, env});
        }
        // whisper size interpolation fix (re-create child correctly for whisper)
        if (target.startsWith('whisper:')) {
          const size=target.split(':')[1];
          child=spawn(PY, ['-c', pyPrefix+`from pathlib import Path;from utils.dependency_manager import setup_faster_whisper_model;app=Path(r'${ROOT.replace(/\\/g,'\\\\')}');ok=setup_faster_whisper_model(app,'${size}');print('DONE:'+str(ok));import sys;sys.exit(0 if ok else 1)`], {cwd: ROOT, env});
        }
        if (!child) return json(res,500,{error:'failed to start'});
        child.stdout.pipe(out); child.stderr.pipe(out);
        const job={proc:child, code:undefined, startedAt:Date.now(), logPath, target};
        DEP_JOBS.set(jobKey, job);
        child.on('close',code=>{ job.code=code; try{ out.end(); }catch{} });
        child.on('error',e=>{ try{ fs.appendFileSync(logPath,'\n[spawn error] '+String(e)+'\n'); }catch{} });
        json(res,200,{ok:true, started:true, target, log: logPath});
      });
      return;
    }
    // GET /api/dependencies/status?target=xxx
    if (p === '/api/dependencies/status' && req.method === 'GET') {
      const target=String(u.searchParams.get('target')||'').trim();
      if (!target) return json(res,400,{error:'target required'});
      const job=DEP_JOBS.get(target);
      if (!job) return json(res,200,{running:false, target});
      const running = job.code===undefined && job.proc && !job.proc.killed;
      json(res,200,{running: !!running, code: job.code, startedAt: job.startedAt, target, log: job.logPath});
      return;
    }
    // GET /api/dependencies/log?target=xxx
    if (p === '/api/dependencies/log' && req.method === 'GET') {
      const target=String(u.searchParams.get('target')||'').trim();
      if (!target) return json(res,400,{error:'target required'});
      const safeTarget=target.replace(/[:\/]/g,'_');
      const logPath=path.join(ROOT,'output',`dep_${safeTarget}.log`);
      // also catch whisper legacy logs
      let altPath=null;
      if (target.startsWith('whisper:')) altPath=path.join(ROOT,'output',`whisper_download_${target.split(':')[1]}.log`);
      let txt='';
      if (fs.existsSync(logPath)) txt=tailFile(logPath, 12000);
      else if (altPath && fs.existsSync(altPath)) txt=tailFile(altPath, 12000);
      json(res,200,{log: txt, target});
      return;
    }
    // GET /api/system - host & runtime overview untuk halaman Dependencies
    if (p === '/api/system' && req.method === 'GET') {
      const si = {
        host: os.hostname(),
        platform: os.platform(),
        arch: os.arch(),
        release: os.release(),
        uptime_s: Math.round(os.uptime()),
        loadavg: os.loadavg(),
        cpu_model: (os.cpus()[0] && os.cpus()[0].model) ? os.cpus()[0].model : '',
        cpu_count: os.cpus().length,
        mem_total: os.totalmem(),
        mem_free: os.freemem(),
        mem_used: os.totalmem() - os.freemem(),
        node: process.version,
        py: PY,
      };
      // disk + python version + pip packages parallel
      Promise.all([
        getDiskStats(),
        new Promise(r => execFile(PY, ['--version'], (e, so, se) => r(((so || se || '').toString().trim() || (e ? String(e.message) : 'unknown'))))),
        new Promise(r => execFile(PY, ['-m', 'pip', '--version'], (e, so) => r(e ? '' : (so || '').toString().split('\n')[0].trim()))),
      ]).then(([disk, pyVer, pipVer]) => {
        si.disk = disk;
        si.python = pyVer;
        si.pip = pipVer;
        // systemd service status (best-effort, non-blocking)
        execFile('systemctl', ['is-active', 'auto-clipper-v2-web.service'], { timeout: 2000 }, (e1, o1) => {
          execFile('systemctl', ['is-active', 'auto-clipper-v2-bot.service'], { timeout: 2000 }, (e2, o2) => {
            si.services = {
              web: (o1 || '').toString().trim() || (e1 ? 'unknown' : 'inactive'),
              bot: (o2 || '').toString().trim() || (e2 ? 'unknown' : 'inactive'),
            };
            json(res, 200, si);
          });
        });
      }).catch(() => json(res, 200, si));
      return;
    }
    // GET /api/python/packages - pip package versions untuk Dependencies
    if (p === '/api/python/packages' && req.method === 'GET') {
      const PYLIST = `import importlib.metadata as m, json; pkgs=["openai","opencv-python","numpy","Pillow","mediapipe","requests","yt-dlp","curl_cffi","faster-whisper","silero-vad","onnxruntime","google-api-python-client","google-auth-oauthlib","python-telegram-bot","telethon","huggingface_hub","certifi","onnxruntime","Pillow"]; out={}; 
for p in ["openai","opencv-python","numpy","Pillow","mediapipe","requests","yt-dlp","curl_cffi","faster-whisper","silero-vad","onnxruntime","google-api-python-client","python-telegram-bot","telethon","huggingface_hub","certifi"]:
 try: out[p]=m.version(p)
 except: out[p]=None
print(json.dumps(out))`;
      execFile(PY, ['-c', PYLIST], (err, stdout) => {
        let map = {};
        try { map = JSON.parse((stdout || '').toString().trim().split('\n').pop() || '{}'); } catch {}
        if (err && !Object.keys(map).length) return json(res, 500, { error: String(err.message || err) });
        const rows = Object.entries(map).map(([name, ver]) => ({ name, version: ver || null, ok: !!ver }));
        json(res, 200, rows);
      });
      return;
    }
    // GET /api/binaries - status dependensi (diperluas untuk Dependencies)
    if (p === '/api/binaries') {
      const findBin = (name, rel) => {
        const probe = isWin ? [rel + '.exe', rel] : [rel];
        for (const cand of probe) if (fs.existsSync(cand)) return { ok: true, detail: cand + ' (bundled)', path: cand };
        for (const dir of (process.env.PATH || '').split(path.delimiter)) {
          for (const cand of probe) {
            const full = path.join(dir, path.basename(cand));
            if (fs.existsSync(full)) return { ok: true, detail: full + ' (PATH)', path: full };
          }
        }
        return { ok: false, detail: 'tidak terdeteksi', path: null };
      };
      const bin = [
        { name: 'ffmpeg', ...findBin('ffmpeg', path.join(ROOT, 'ffmpeg', 'ffmpeg')) },
        { name: 'ffprobe', ...findBin('ffprobe', path.join(ROOT, 'ffmpeg', 'ffprobe')) },
        { name: 'deno', ...findBin('deno', path.join(ROOT, 'bin', 'deno')) },
        { name: 'rclone', ...findBin('rclone', '/usr/bin/rclone') },
      ];
      // enrich with version strings (best-effort, never fail the endpoint)
      const verOf = (binPath, args) => new Promise(r => {
        if (!binPath) return r('');
        execFile(binPath, args, { timeout: 4000 }, (e, so) => {
          if (e) return r('');
          const line = (so || '').toString().split('\n')[0].trim();
          r(line.slice(0, 120));
        });
      });
      Promise.all([
        verOf(bin.find(b => b.name === 'ffmpeg')?.path, ['-version']),
        verOf(bin.find(b => b.name === 'ffprobe')?.path, ['-version']),
        verOf(bin.find(b => b.name === 'deno')?.path, ['--version']),
        verOf(bin.find(b => b.name === 'rclone')?.path, ['version']),
        new Promise(r => execFile(PY, ['-c', 'import yt_dlp;print(yt_dlp.version.__version__)'], (e, so) => r(e ? '' : 'v' + (so || '').toString().trim()))),
        new Promise(r => execFile(PY, ['-c', 'import sys;print(sys.version.split()[0])'], (e, so) => r(e ? '' : (so || '').toString().trim()))),
        new Promise(r => execFile(PY, ['-c', 'import importlib.metadata; print(importlib.metadata.version("gdown"))'], (e, so) => r(e ? '' : (so || '').toString().trim()))),
        new Promise(r => {
          const PYCHK = `from pathlib import Path;import sys;sys.path.insert(0, r'${ROOT.replace(/\\/g,'\\\\')}');from utils.dependency_manager import check_dependency;import json;app=Path(r'${ROOT.replace(/\\/g,'\\\\')}');print(json.dumps(check_dependency('mediapipe_model', app)))`;
          execFile(PY, ['-c', PYCHK], (e, so) => {
            let ok = false; try { ok = JSON.parse((so || '').toString().trim().split('\n').pop() || 'false'); } catch {}
            r(ok);
          });
        }),
      ]).then(([ffVer, fpVer, denoVer, rcloneVer, ytdlpVer, pyVer, gdownVer, mpOk]) => {
        const byName = Object.fromEntries(bin.map(b => [b.name, b]));
        if (byName.ffmpeg) byName.ffmpeg.version = ffVer || (byName.ffmpeg.ok ? byName.ffmpeg.detail : '');
        if (byName.ffprobe) byName.ffprobe.version = fpVer || (byName.ffprobe.ok ? byName.ffprobe.detail : '');
        if (byName.deno) byName.deno.version = denoVer ? denoVer.split('\n')[0] : (byName.deno.ok ? byName.deno.detail : '');
        if (byName.rclone) byName.rclone.version = rcloneVer ? rcloneVer.split('\n')[0].slice(0,120) : (byName.rclone.ok ? byName.rclone.detail : '');
        // yt-dlp as module (not a binary file)
        const ytdlp = { name: 'yt-dlp', ok: !!ytdlpVer, detail: ytdlpVer ? ytdlpVer + ' (module)' : 'tidak terdeteksi', path: null, version: ytdlpVer || '' };
        const py = { name: 'python', ok: !!pyVer, detail: PY + (pyVer ? ' v' + pyVer : ''), path: PY, version: pyVer ? 'v' + pyVer : '' };
        const node = { name: 'node', ok: true, detail: process.version + ' (' + process.execPath + ')', path: process.execPath, version: process.version };
        let mpDetail = 'tidak terdeteksi';
        let mpPath = path.join(ROOT, 'bin', 'face_landmarker.task');
        try { const st = fs.statSync(mpPath); mpDetail = mpOk ? (st.size / 1048576).toFixed(1) + ' MB - ' + mpPath : 'tidak terdeteksi'; } catch { mpDetail = mpOk ? mpPath : 'tidak terdeteksi'; }
        const mp = { name: 'mediapipe', ok: !!mpOk, detail: mpDetail, path: mpOk ? mpPath : null, version: mpOk ? mpDetail : '' };
        const gdown = { name: 'gdown', ok: !!gdownVer, detail: gdownVer ? 'v' + gdownVer + ' (pip)' : 'tidak terdeteksi (pip install gdown)', path: null, version: gdownVer ? 'v' + gdownVer : '' };
        const out = [byName.ffmpeg, byName.ffprobe, byName.deno, byName.rclone, ytdlp, gdown, py, node, mp].filter(Boolean);
        json(res, 200, out);
      }).catch(() => {
        // fallback minimal
        execFile(PY, ['-c', 'import yt_dlp;print(yt_dlp.version.__version__)'], (err, stdout) => {
          bin.push({ name: 'yt-dlp', ok: !err, detail: err ? 'tidak terdeteksi' : 'v' + (stdout || '').toString().trim() + ' (module)', path: null, version: err ? '' : 'v' + (stdout || '').toString().trim() });
          json(res, 200, bin);
        });
      });
      return;
    }
    // POST /api/render/:session/:clipDir - re-render klip dgn config aktif (async + log)
    const mRen = p.match(/^\/api\/render\/([^/]+)\/([^/]+)$/);
    if (mRen && req.method === 'POST') {
      const sessDir = path.join(SESSIONS, safe(mRen[1]));
      const clipDir = path.join(sessDir, 'clips', safe(mRen[2]));
      if (!clipDir.startsWith(SESSIONS) || !fs.existsSync(clipDir)) return json(res, 404, { error: 'not found' });
      if (!fs.existsSync(path.join(clipDir, 'landscape.mp4'))) return json(res, 400, { error: 'landscape.mp4 tidak ada - sumber render hilang' });
      const key = `${mRen[1]}/${mRen[2]}`;
      const prev = RENDER_JOBS.get(key);
      if (prev && !prev.proc.killed && prev.code === undefined) return json(res, 409, { error: 'render untuk klip ini masih berjalan' });
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        let opt = {};
        try { opt = JSON.parse(body || '{}'); } catch {}
        const env = { ...process.env, RENDER_OPTS: JSON.stringify(opt) };
        const logPath = path.join(clipDir, 'render.log');
        fs.appendFileSync(logPath, `\n===== render start ${new Date().toISOString()} opts=${JSON.stringify(opt)} =====\n`);
        const out = fs.createWriteStream(logPath, { flags: 'a' });
        const child = spawn(PY, [path.join(__dirname, 'render_clip.py'), sessDir, clipDir], { env });
        child.stdout.pipe(out);
        child.stderr.pipe(out);
        const job = { proc: child, code: undefined, startedAt: Date.now() };
        RENDER_JOBS.set(key, job);
        child.on('close', code => { job.code = code; job.finishedAt = Date.now(); out.end(); });
        json(res, 200, { ok: true, started: true });
      });
      return;
    }
    // GET /api/render/status/:session/:clipDir - status + tail log
    const mRst = p.match(/^\/api\/render\/status\/([^/]+)\/([^/]+)$/);
    if (mRst) {
      const clipDir = path.join(SESSIONS, safe(mRst[1]), 'clips', safe(mRst[2]));
      if (!clipDir.startsWith(SESSIONS)) return json(res, 403, { error: 'forbidden' });
      const key = `${mRst[1]}/${mRst[2]}`;
      const job = RENDER_JOBS.get(key);
      const logPath = path.join(clipDir, 'render.log');
      let log = '';
      try {
        const stat = fs.existsSync(logPath) ? fs.statSync(logPath).size : 0;
        if (stat > 0) {
          const fd = fs.openSync(logPath, 'r');
          const buf = Buffer.alloc(Math.min(stat, 12000));
          fs.readSync(fd, buf, 0, buf.length, Math.max(0, stat - buf.length));
          fs.closeSync(fd);
          log = buf.toString('utf8');
        }
      } catch {}
      return json(res, 200, {
        running: !!(job && job.code === undefined),
        code: job ? job.code : null,
        elapsed_s: job ? Math.round(((job.code !== undefined && job.finishedAt ? job.finishedAt : Date.now()) - job.startedAt) / 1000) : null,
        log,
        progress: parseOverall(log),
      });
    }
    // POST /api/create - phase 1: subtitle + AI highlights (seperti bot)
    if (p === '/api/create' && req.method === 'POST') {
      if (CREATE_JOB && CREATE_JOB.code === undefined) return json(res, 409, { error: 'Analisis masih berjalan' });
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        let o = {};
        try { o = JSON.parse(body || '{}'); } catch {}
        if (!o.url || !/^https?:\/\//.test(o.url)) return json(res, 400, { error: 'URL tidak valid' });
        // ponytail: log per-run biar debug create hanya run ini (simpan 10 terakhir)
        const logPath = path.join(ROOT, 'output', `create_phase1_${Date.now()}.log`);
        try {
          const olds = fs.readdirSync(path.join(ROOT, 'output')).filter(f => /^create_phase1_\d+\.log$/.test(f)).sort();
          while (olds.length >= 10) fs.unlinkSync(path.join(ROOT, 'output', olds.shift()));
        } catch {}
        const out = fs.createWriteStream(logPath, { flags: 'a' });
        const resultFile = path.join(ROOT, 'output', `.phase1_result_${Date.now()}.json`);
        const child = spawn(PY, [path.join(__dirname, 'phase1_create.py'), String(o.url), String(parseInt(o.num_clips) || 0), resultFile], { detached: true, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
        child.stdout.pipe(out); child.stderr.pipe(out);
        CREATE_JOB = { proc: child, code: undefined, startedAt: Date.now(), resultFile, logPath, url: String(o.url) };
        child.on('close', code => { CREATE_JOB.code = code; CREATE_JOB.finishedAt = Date.now(); out.end(); });
        json(res, 200, { ok: true, started: true });
      });
      return;
    }
    // GET /api/create/status
    if (p === '/api/create/status') {
      const j = CREATE_JOB;
      let result = null;
      try { if (j && j.code !== undefined && fs.existsSync(j.resultFile)) result = JSON.parse(fs.readFileSync(j.resultFile, 'utf8')); } catch {}
      return json(res, 200, {
        running: !!(j && j.code === undefined),
        code: j ? j.code : null,
        elapsed_s: j ? Math.round(((j.code !== undefined && j.finishedAt ? j.finishedAt : Date.now()) - j.startedAt) / 1000) : null,
        url: j ? j.url : null,
        log: j && j.logPath ? tailFile(j.logPath) : '',
        progress: j && j.logPath ? parseOverall(tailFile(j.logPath)) : null,
        result,
      });
    }
    // POST /api/create/cancel - hentikan analisis berjalan
    if (p === '/api/create/cancel' && req.method === 'POST') {
      const j = CREATE_JOB;
      if (!j || j.code !== undefined) return json(res, 404, { error: 'Tidak ada analisis berjalan' });
      try { process.kill(-j.proc.pid, 'SIGKILL'); } catch { try { j.proc.kill('SIGKILL'); } catch {} }
      return json(res, 200, { ok: true, cancelled: true });
    }
    // POST /api/upload/watermark - simpan gambar watermark ke assets/watermarks + set config
    if (p === '/api/upload/watermark' && req.method === 'POST') {
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        let o = {};
        try { o = JSON.parse(body || '{}'); } catch {}
        const b64 = String(o.data || '').replace(/^data:[^;]+;base64,/, '').trim();
        if (!b64) return json(res, 400, { error: 'Tidak ada data gambar' });
        const buf = Buffer.from(b64, 'base64');
        if (!buf.length || buf.length > 8 * 1024 * 1024) return json(res, 400, { error: 'Ukuran file 0 atau > 8 MB' });
        let name = String(o.name || 'watermark.png').replace(/[^A-Za-z0-9._-]/g, '_');
        if (!/\.(png|jpg|jpeg)$/i.test(name)) name += '.png';
        try {
          const dir = path.join(ROOT, 'assets', 'watermarks');
          fs.mkdirSync(dir, { recursive: true });
          const dest = path.join(dir, name);
          fs.writeFileSync(dest, buf);
          const fp = path.join(ROOT, 'config.json');
          const cfg = JSON.parse(fs.readFileSync(fp, 'utf8'));
          cfg.watermark = cfg.watermark || {};
          cfg.watermark.image_path = dest;
          const tmp = fp + '.tmp';
          fs.writeFileSync(tmp, JSON.stringify(cfg, null, 2));
          fs.renameSync(tmp, fp);
          json(res, 200, { ok: true, path: dest });
        } catch (e) { json(res, 500, { error: String(e) }); }
      });
      return;
    }
    // --- Cookies management (yt-dlp): upload, status, hapus, test ---
    const COOKIES_FILE = path.join(ROOT, 'cookies.txt');
    const cookiesInfo = () => {
      try {
        const st = fs.statSync(COOKIES_FILE);
        const txt = fs.readFileSync(COOKIES_FILE, 'utf8');
        const lines = txt.split('\n').filter(l => l.trim() && !l.trim().startsWith('#')).length;
        return { exists: true, size: st.size, lines, mtime: st.mtimeMs,
          domains: [...new Set(txt.split('\n').map(l => l.trim().split('\t')[0]).filter(d => d && !d.startsWith('#') && !d.startsWith('http')))].slice(0, 10) };
      } catch { return { exists: false }; }
    };
    // POST /api/cookies - upload cookies.txt (base64 content)
    if (p === '/api/cookies' && req.method === 'POST') {
      let body = '';
      req.on('data', c => body += c); req.on('error', () => {});
      req.on('end', () => {
        try {
          const o = JSON.parse(body || '{}');
          const b64 = String(o.data || '').trim();
          if (!b64) return json(res, 400, { error: 'Tidak ada data cookies' });
          let txt = Buffer.from(b64, 'base64').toString('utf8');
          if (!/^# (HTTP )?Cookie/m.test(txt) && !txt.includes('\t') && txt.includes('yt-dlp')) {
            return json(res, 400, { error: 'Format bukan cookies.txt (Netscape)' });
          }
          if (!txt.match(/# (HTTP )?Cookie/)) {
            // tambahkan header Netscape bila belum ada (agar yt-dlp mau baca)
            txt = '# Netscape HTTP Cookie File\n# http://curl.haxx.se/rfc/cookie_spec.html\n# This file was generated by AutoClipper web panel\n' + txt;
          }
          fs.writeFileSync(COOKIES_FILE, txt, 'utf8');
          json(res, 200, { ok: true, ...cookiesInfo() });
        } catch (e) { json(res, 500, { error: String(e) }); }
      });
      return;
    }
    // GET /api/cookies - status cookies
    if (p === '/api/cookies' && req.method === 'GET') {
      return json(res, 200, cookiesInfo());
    }
    // DELETE /api/cookies - hapus cookies
    if (p === '/api/cookies' && req.method === 'DELETE') {
      try { if (fs.existsSync(COOKIES_FILE)) fs.rmSync(COOKIES_FILE); json(res, 200, { ok: true, exists: false }); }
      catch (e) { json(res, 500, { error: String(e) }); }
      return;
    }
    // POST /api/cookies/test - uji cookies terhadap video YouTube
    if (p === '/api/cookies/test' && req.method === 'POST') {
      let body = '';
      req.on('data', c => body += c); req.on('end', () => {
        let o = {}; try { o = JSON.parse(body || '{}'); } catch {}
        if (!fs.existsSync(COOKIES_FILE)) return json(res, 400, { error: 'Belum ada cookies' });
        const url = String(o.url || 'https://www.youtube.com/watch?v=jNQXAC9IVRw').trim() || 'https://www.youtube.com/watch?v=jNQXAC9IVRw';
        const args = ['--cookies', COOKIES_FILE, '--dump-single-json', '--no-warnings', '--skip-download', '--socket-timeout', '15', url];
        const child = execFile('/usr/local/bin/yt-dlp', args, { timeout: 60000, maxBuffer: 8 * 1024 * 1024 }, (err, stdout, stderr) => {
          if (err) {
            const msg = String(stderr || err.message || '');
            const needsAuth = /Sign in to confirm|confirm your identity|Sign in to YouTube|LOGIN_REQUIRED|"status": *"fail"/i.test(msg);
            return json(res, 200, {
              ok: false,
              auth: needsAuth ? 'gagal' : 'tidak-yakin',
              message: needsAuth
                ? 'Cookies TIDAK valid - yt-dlp butuh login (YouTube minta verifikasi). Export cookies baru dari browser yang sudah login.'
                : 'yt-dlp gagal menjalankan test: ' + msg.split('\n').slice(-3).join(' '),
              detail: msg.split('\n').slice(-8),
            });
          }
          let info = null; try { info = JSON.parse(stdout); } catch {}
          return json(res, 200, {
            ok: true,
            auth: 'ok',
            message: 'Cookies VALID. Login berhasil.',
            title: info && info.title, channel: info && (info.channel || info.uploader), id: info && info.id,
          });
        });
      });
      return;
    }
    // --- Auto BGM management: daftar/upload/hapus file musik per mood ---
    const BGM_MOODS = ['chill', 'epic', 'sad', 'upbeat', 'suspense'];
    const BGM_DIR = path.join(ROOT, 'assets', 'bgm');
    const BGM_EXT = ['.mp3', '.m4a', '.wav', '.aac', '.ogg'];
    const listBgm = () => {
      const out = {};
      for (const mood of BGM_MOODS) {
        const d = path.join(BGM_DIR, mood);
        let files = [];
        try {
          files = fs.readdirSync(d)
            .filter(f => BGM_EXT.includes(path.extname(f).toLowerCase()))
            .map(f => {
              const fp = path.join(d, f);
              let size = 0; try { size = fs.statSync(fp).size; } catch {}
              return { name: f, size };
            })
            .sort((a, b) => a.name.localeCompare(b.name));
        } catch { files = []; }
        out[mood] = files;
      }
      return out;
    };
    // GET /api/bgm - daftar file BGM per mood + toggle status config
    if (p === '/api/bgm' && req.method === 'GET') {
      try {
        const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json'), 'utf8'));
        json(res, 200, { moods: BGM_MOODS, bgm: listBgm(), enabled: !!(cfg.auto_bgm && cfg.auto_bgm.enabled) });
      } catch (e) { json(res, 500, { error: String(e) }); }
      return;
    }
    // POST /api/bgm - upload file BGM { mood, name, data(base64) }
    if (p === '/api/bgm' && req.method === 'POST') {
      let body = '';
      req.on('data', c => body += c); req.on('error', () => {});
      req.on('end', () => {
        try {
          const o = JSON.parse(body || '{}');
          const mood = String(o.mood || '').toLowerCase();
          if (!BGM_MOODS.includes(mood)) return json(res, 400, { error: 'Mood tidak valid: ' + mood });
          const b64 = String(o.data || '').replace(/^data:[^;]+;base64,/, '').trim();
          if (!b64) return json(res, 400, { error: 'Tidak ada data audio' });
          const buf = Buffer.from(b64, 'base64');
          if (!buf.length) return json(res, 400, { error: 'File kosong' });
          if (buf.length > 50 * 1024 * 1024) return json(res, 400, { error: 'File > 50 MB' });
          let name = String(o.name || 'bgm.mp3').replace(/[^A-Za-z0-9._-]/g, '_');
          const ext = path.extname(name).toLowerCase();
          if (!BGM_EXT.includes(ext)) { name = name + '.mp3'; }
          const dir = path.join(BGM_DIR, mood);
          fs.mkdirSync(dir, { recursive: true });
          const dest = path.join(dir, name);
          fs.writeFileSync(dest, buf);
          json(res, 200, { ok: true, mood, name, size: buf.length });
        } catch (e) { json(res, 500, { error: String(e) }); }
      });
      return;
    }
    // DELETE /api/bgm/:mood/:file - hapus file BGM
    const mBgmDel = p.match(/^\/api\/bgm\/([^/]+)\/([^/]+)$/);
    if (mBgmDel && req.method === 'DELETE') {
      try {
        const mood = safe(mBgmDel[1]).toLowerCase();
        if (!BGM_MOODS.includes(mood)) return json(res, 400, { error: 'Mood tidak valid' });
        const name = path.basename(safe(mBgmDel[2]));
        const fp = path.join(BGM_DIR, mood, name);
        if (!fp.startsWith(BGM_DIR) || !fs.existsSync(fp)) return json(res, 404, { error: 'File tidak ditemukan' });
        fs.rmSync(fp);
        json(res, 200, { ok: true });
      } catch (e) { json(res, 500, { error: String(e) }); }
      return;
    }
    // --- Transition Library management: daftar pool, download, cache, config ---
    const TRANS_CACHE = path.join(ROOT, 'transitions_cache');
    const TRANS_POOL_INFO = [
      { url: 'https://www.youtube.com/watch?v=yfKv03nLaBE', type: 'film_burn', orientation: 'landscape', label: 'GRUNGY Film Burn Transitions' },
      { url: 'https://www.youtube.com/watch?v=uYBcUpLxtEM', type: 'film_burn', orientation: 'landscape', label: 'GRUNGE Film Overlay with Sound' },
      { url: 'https://www.youtube.com/watch?v=YFzGx0JuUUQ', type: 'film_overlay', orientation: 'landscape', label: '35mm Film Overlay' },
      { url: 'https://www.youtube.com/watch?v=iGvnBXS3pyM', type: 'film_leader', orientation: 'landscape', label: 'Dirty Grainy Film Leader' },
      { url: 'https://www.youtube.com/watch?v=BsKj9iiimTE', type: 'film_leader', orientation: 'landscape', label: 'Classic Film Leader Overlays' },
      { url: 'https://www.youtube.com/watch?v=OaK3jjBfOi0', type: 'film_grain', orientation: 'landscape', label: 'Film Grain Overlay with Sound Effect' },
      { url: 'https://www.youtube.com/watch?v=k0BvSreLx5E', type: 'film_burn', orientation: 'vertical', label: 'Vertical Vibrant Film Burn Overlay' },
      { url: 'https://www.youtube.com/watch?v=eiditSLUA3I', type: 'film_burn', orientation: 'vertical', label: 'Vertical Rich and Vibrant Colors' },
    ];
    const listTransCache = () => {
      try { return fs.readdirSync(TRANS_CACHE).filter(f => /\.(mp4|webm|mkv)$/i.test(f)).map(f => { let s=0; try{s=fs.statSync(path.join(TRANS_CACHE,f)).size;}catch{}; return {name:f, size:s}; }); }
      catch { return []; }
    };
    // GET /api/transitions - daftar pool + status cache + config
    if (p === '/api/transitions' && req.method === 'GET') {
      try {
        const cache = listTransCache();
        const cachedIds = new Set(cache.map(f => (f.name.match(/^tmp_raw_(.+?)\.mp4/)||[])[1]).filter(Boolean));
        const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json'), 'utf8'));
        const pool = TRANS_POOL_INFO.map(e => ({ ...e, cached: cachedIds.has(e.url.split('v=')[1].split('&')[0]) }));
        json(res, 200, { pool, cache, enabled: !!(cfg.transition_library && cfg.transition_library.enabled), style: (cfg.transition_library && cfg.transition_library.style) || 'random' });
      } catch (e) { json(res, 500, { error: String(e) }); }
      return;
    }
    // POST /api/transitions/download - download semua transisi ke cache (async)
    if (p === '/api/transitions/download' && req.method === 'POST') {
      fs.mkdirSync(TRANS_CACHE, { recursive: true });
      const logPath = path.join(ROOT, 'output', 'transitions_download.log');
      fs.mkdirSync(path.dirname(logPath), { recursive: true });
      const out = fs.createWriteStream(logPath, { flags: 'a' });
      fs.appendFileSync(logPath, `\n===== transition download start ${new Date().toISOString()} =====\n`);
      const child = spawn(PY, ['-c', 'from core.transition import download_all_transitions; download_all_transitions()'], { cwd: ROOT });
      child.stdout.pipe(out); child.stderr.pipe(out);
      TRANS_JOB = { proc: child, startedAt: Date.now() };
      child.on('close', code => { TRANS_JOB.code = code; TRANS_JOB.finishedAt = Date.now(); out.end(); });
      json(res, 200, { ok: true, started: true });
      return;
    }
    // GET /api/transitions/status - status download + log
    if (p === '/api/transitions/status' && req.method === 'GET') {
      return json(res, 200, {
        running: !!(TRANS_JOB && TRANS_JOB.code === undefined),
        code: TRANS_JOB ? TRANS_JOB.code : null,
        log: tailFile(path.join(ROOT, 'output', 'transitions_download.log'), 6000),
        cache: listTransCache(),
      });
    }
    // DELETE /api/transitions/cache - bersihkan cache transisi
    if (p === '/api/transitions/cache' && req.method === 'DELETE') {
      try {
        if (fs.existsSync(TRANS_CACHE)) { for (const f of fs.readdirSync(TRANS_CACHE)) fs.rmSync(path.join(TRANS_CACHE, f), { force: true }); }
        json(res, 200, { ok: true });
      } catch (e) { json(res, 500, { error: String(e) }); }
      return;
    }
    // --- Thumbnail management: generate dari klip, daftar, hapus, toggle ---
    const THUMB_DIR = path.join(ROOT, 'output', 'thumbnails');
    const listThumbs = () => {
      try {
        return fs.readdirSync(THUMB_DIR).filter(f => /\.(png|jpe?g)$/i.test(f)).map(f => {
          const fp = path.join(THUMB_DIR, f); let s=0; try{s=fs.statSync(fp).size;}catch{};
          return { name: f, size: s, url: '/api/thumbnails/file/' + encodeURIComponent(f) };
        });
      } catch { return []; }
    };
    // GET /api/thumbnails - daftar + config
    if (p === '/api/thumbnails' && req.method === 'GET') {
      try {
        const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json'), 'utf8'));
        fs.mkdirSync(THUMB_DIR, { recursive: true });
        json(res, 200, { list: listThumbs(), enabled: !!(cfg.thumbnail && cfg.thumbnail.enabled) });
      } catch (e) { json(res, 500, { error: String(e) }); }
      return;
    }
    // POST /api/thumbnails/generate - { session, clipDir }
    if (p === '/api/thumbnails/generate' && req.method === 'POST') {
      let body = '';
      req.on('data', c => body += c); req.on('end', () => {
        let o = {}; try { o = JSON.parse(body || '{}'); } catch {}
        const sess = String(o.session || ''), clipDir = String(o.clipDir || '');
        if (!sess || !clipDir) return json(res, 400, { error: 'session & clipDir wajib' });
        const clipPath = path.join(SESSIONS, sess, 'clips', clipDir);
        if (!clipPath.startsWith(SESSIONS) || !fs.existsSync(clipPath)) return json(res, 404, { error: 'klip tidak ditemukan' });
        fs.mkdirSync(THUMB_DIR, { recursive: true });
        const outName = (sess + '_' + clipDir + '_' + Date.now() + '.jpg').replace(/[^A-Za-z0-9_.-]/g, '_');
        const outPath = path.join(THUMB_DIR, outName);
        const child = execFile(PY, [path.join(__dirname, 'gen_thumbnail.py'), sess, clipDir, outPath, String(o.frame_ms || 5000), String(o.alpha ?? 128)],
          { cwd: ROOT, timeout: 60000, maxBuffer: 8*1024*1024 }, (err, stdout, stderr) => {
            if (err || !fs.existsSync(outPath)) {
              const msg = String(stderr || err.message || '');
              return json(res, 500, { error: msg.split('\n').filter(Boolean).slice(-3).join(' ') || 'gagal generate' });
            }
            let info = {}; try { info = JSON.parse(stdout); } catch {}
            return json(res, 200, { ok: true, name: outName, url: '/api/thumbnails/file/' + encodeURIComponent(outName), title: info.title });
          });
      });
      return;
    }
    // GET /api/thumbnails/file/:name - sajikan file thumbnail
    const mThumbFile = p.match(/^\/api\/thumbnails\/file\/([^/]+)$/);
    if (mThumbFile) {
      const name = path.basename(safe(mThumbFile[1]));
      const fp = path.join(THUMB_DIR, name);
      if (!fp.startsWith(THUMB_DIR) || !fs.existsSync(fp)) return json(res, 404, { error: 'not found' });
      const ext = path.extname(name).toLowerCase();
      res.writeHead(200, { 'Content-Type': ext === '.png' ? 'image/png' : 'image/jpeg', 'Content-Length': fs.statSync(fp).size, 'Cache-Control': 'no-store' });
      fs.createReadStream(fp).pipe(res);
      return;
    }
    // DELETE /api/thumbnails/file/:name - hapus thumbnail
    const mThumbDel = p.match(/^\/api\/thumbnails\/file\/([^/]+)$/);
    if (mThumbDel && req.method === 'DELETE') {
      try {
        const name = path.basename(safe(mThumbDel[1]));
        const fp = path.join(THUMB_DIR, name);
        if (fp.startsWith(THUMB_DIR) && fs.existsSync(fp)) fs.rmSync(fp);
        json(res, 200, { ok: true });
      } catch (e) { json(res, 500, { error: String(e) }); }
      return;
    }
    // POST /api/refind/:session - regenerate highlights sesi ada (async + log)
    const mRef = p.match(/^\/api\/refind\/([^/]+)$/);
    if (mRef && req.method === 'POST') {
      const sid = safe(mRef[1]);
      if (!fs.existsSync(path.join(SESSIONS, sid, 'session_data.json'))) return json(res, 404, { error: 'session tidak ditemukan' });
      const prev = REFIND_JOBS.get(sid);
      if (prev && prev.code === undefined) return json(res, 409, { error: 'Regenerate untuk sesi ini masih berjalan' });
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        let o = {};
        try { o = JSON.parse(body || '{}'); } catch {}
        const resultFile = path.join(ROOT, 'output', `.refind_result_${Date.now()}.json`);
        const logPath = path.join(SESSIONS, sid, 'refind.log');
        fs.mkdirSync(path.dirname(logPath), { recursive: true });
        fs.appendFileSync(logPath, `\n===== refind start ${new Date().toISOString()} n=${parseInt(o.num_clips) || 0} =====\n`);
        const out = fs.createWriteStream(logPath, { flags: 'a' });
        const child = spawn(PY, [path.join(__dirname, 'refind_highlights.py'), sid, String(parseInt(o.num_clips) || 0), resultFile]);
        child.stdout.pipe(out); child.stderr.pipe(out);
        const job = { proc: child, code: undefined, startedAt: Date.now(), resultFile };
        REFIND_JOBS.set(sid, job);
        child.on('close', code => { job.code = code; job.finishedAt = Date.now(); out.end(); });
        json(res, 200, { ok: true, started: true });
      });
      return;
    }
    // GET /api/refind/status/:session
    const mRfd = p.match(/^\/api\/refind\/status\/([^/]+)$/);
    if (mRfd) {
      const sid = safe(mRfd[1]);
      const job = REFIND_JOBS.get(sid);
      let result = null;
      try { if (job && job.code !== undefined && fs.existsSync(job.resultFile)) result = JSON.parse(fs.readFileSync(job.resultFile, 'utf8')); } catch {}
      return json(res, 200, {
        running: !!(job && job.code === undefined),
        code: job ? job.code : null,
        elapsed_s: job ? Math.round(((job.code !== undefined && job.finishedAt ? job.finishedAt : Date.now()) - job.startedAt) / 1000) : null,
        log: tailFile(path.join(SESSIONS, sid, 'refind.log'), 6000),
        progress: parseOverall(tailFile(path.join(SESSIONS, sid, 'refind.log'), 6000)),
        result,
      });
    }
    // GET /api/dashboard - ringkasan agregat: total klip, viral tertinggi, story outputs, klip terbaru
        if (p === '/api/dashboard') {
          try {
            const sessions = listSessions(); // pakai cache 1.5s, bukan _listSessions raw scan
            const allClips = sessions.flatMap(s => (s.clips || []).map(c => ({ ...c, session: s.id, sessionTitle: s.title })));
            const scored = allClips.filter(c => c.score != null).sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, 5);
            const recent = allClips.slice().sort((a, b) => (b.created || 0) - (a.created || 0)).slice(0, 5);
            let storyOutputs = [];
            try {
              const base = path.join(ROOT, 'output', 'story_clips');
              if (fs.existsSync(base)) {
                const now = Date.now();
                if (DASH_STORY_CACHE.t && now - DASH_STORY_CACHE.t < 5000) {
                  storyOutputs = DASH_STORY_CACHE.data;
                } else {
                  storyOutputs = fs.readdirSync(base).filter(d => {
                    try { return fs.statSync(path.join(base, d)).isDirectory(); } catch { return false; }
                  }).sort().flatMap(d => {
                    const dir = path.join(base, d);
                    return fs.readdirSync(dir).filter(f => f.toLowerCase().endsWith('.mp4')).map(f => ({
                      clip: d, file: f,
                      url: '/video/story/' + encodeURIComponent(d) + '/' + encodeURIComponent(f),
                      size_bytes: fs.statSync(path.join(dir, f)).size,
                      mtime: fs.statSync(path.join(dir, f)).mtimeMs,
                    }));
                  }).sort((a, b) => (b.mtime || 0) - (a.mtime || 0)).slice(0, 5);
                  DASH_STORY_CACHE.t = now;
                  DASH_STORY_CACHE.data = storyOutputs;
                }
              }
            } catch {}
            let fb = { count: 0, lastStatus: null };
            try {
              const fp = path.join(ROOT, 'output', 'fb_upload_results.json');
              if (fs.existsSync(fp)) {
                const rows = JSON.parse(fs.readFileSync(fp, 'utf8'));
                fb.count = Array.isArray(rows) ? rows.length : 0;
                fb.lastStatus = Array.isArray(rows) && rows.length ? rows[rows.length - 1] : null;
              }
            } catch {}
            return json(res, 200, {
              total_sessions: sessions.length,
              total_clips: allClips.length,
              top_viral: scored,
              recent: recent,
              story: storyOutputs,
              fb,
            });
          } catch (e) { return json(res, 500, { error: String(e) }); }
        }
    // POST /api/story/run - jalankan Story Clip pipeline (multi-source) async
    if (p === '/api/story/run' && req.method === 'POST') {
      const prev = STORY_JOBS.get('run');
      if (prev && prev.code === undefined) return json(res, 409, { error: 'Story pipeline masih berjalan' });
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        let o = {};
        try { o = JSON.parse(body || '{}'); } catch {}
        const appDir = ROOT;
        const outDir = path.join(appDir, 'output');
        const storyDir = path.join(outDir, 'story');
        const pick = (v, def) => { try { const r = v && String(v).trim() ? path.resolve(appDir, String(v).trim()) : ''; if (r && (r === ROOT || r.startsWith(ROOT + path.sep)) && fs.existsSync(r)) return r; } catch {} return def; };
        const sourcesJson = pick(o.sources, path.join(storyDir, 'sources.json'));
        const recipeJson = pick(o.recipe, path.join(storyDir, 'story_recipe.json'));
        if (!fs.existsSync(sourcesJson) || !fs.existsSync(recipeJson)) {
          return json(res, 400, { error: `sources.json / story_recipe.json dibutuhkan` });
        }
        let cfgSt = {};
        try { cfgSt = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json'), 'utf8')); } catch {}
        const resultFile = path.join(outDir, `.story_result_${Date.now()}.json`);
        const logPath = path.join(outDir, `story_${Date.now()}.log`);
        const opts = JSON.stringify({
          sources_json: sourcesJson,
          recipe_json: recipeJson,
          outputs_dir: outDir,
          whisper_model: o.whisper_model || (cfgSt.story_clip && cfgSt.story_clip.whisper_model) || 'medium',
          skip_download: !!o.skip_download,
          download_height: o.download_height || 'max',
          ratio: o.ratio || '9:16',
        });
        const out = fs.createWriteStream(logPath, { flags: 'a' });
        const child = spawn(PY, [path.join(__dirname, 'story_run.py'), resultFile], { env: { ...process.env, PYTHONIOENCODING: 'utf-8', STORY_OPTS: opts } });
        child.stdout.pipe(out); child.stderr.pipe(out);
        STORY_JOBS.set('run', { proc: child, code: undefined, startedAt: Date.now(), resultFile, logPath });
        child.on('close', code => { const j = STORY_JOBS.get('run'); if (j) { j.code = code; j.finishedAt = Date.now(); } out.end(); });
        json(res, 200, { ok: true, started: true, log: logPath });
      });
      return;
    }
    // GET /api/story/status
    if (p === '/api/story/status') {
      const j = STORY_JOBS.get('run');
      let result = null;
      try { if (j && j.code !== undefined && fs.existsSync(j.resultFile)) result = JSON.parse(fs.readFileSync(j.resultFile, 'utf8')); } catch {}
      return json(res, 200, {
        running: !!(j && j.code === undefined),
        code: j ? j.code : null,
        elapsed_s: j ? Math.round(((j.code !== undefined && j.finishedAt ? j.finishedAt : Date.now()) - j.startedAt) / 1000) : null,
        log: j && j.logPath ? tailFile(j.logPath) : '',
        progress: j && j.logPath ? parseOverall(tailFile(j.logPath)) : null,
        result,
      });
    }
    // POST /api/story/cancel
    if (p === '/api/story/cancel' && req.method === 'POST') {
      const j = STORY_JOBS.get('run');
      if (!j || j.code !== undefined) return json(res, 404, { error: 'Tidak ada story pipeline berjalan' });
      try { process.kill(-j.proc.pid, 'SIGKILL'); } catch { try { j.proc.kill('SIGKILL'); } catch {} }
      return json(res, 200, { ok: true, cancelled: true });
    }
    // GET /api/story/outputs - daftar hasil dari output/story_clips
    if (p === '/api/story/outputs') {
      try {
        const base = path.join(ROOT, 'output', 'story_clips');
        if (!fs.existsSync(base)) return json(res, 200, []);
        const list = fs.readdirSync(base).filter(d => fs.statSync(path.join(base, d)).isDirectory()).sort().flatMap(d => {
          try {
            const dir = path.join(base, d);
            return fs.readdirSync(dir).filter(f => f.toLowerCase().endsWith('.mp4')).map(f => {
              const fp = path.join(dir, f);
              return { clip: d, file: f, url: '/video/story/' + encodeURIComponent(d) + '/' + encodeURIComponent(f), size_bytes: fs.statSync(fp).size, mtime: fs.statSync(fp).mtimeMs };
            });
          } catch { return []; }
        });
        return json(res, 200, list);
      } catch (e) { return json(res, 500, { error: String(e) }); }
    }
    // POST /api/fb/upload - jalankan Facebook Reels uploader async
    if (p === '/api/fb/upload' && req.method === 'POST') {
      const prev = FB_JOBS.get('run');
      if (prev && prev.code === undefined) return json(res, 409, { error: 'Facebook upload masih berjalan' });
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        let o = {};
        try { o = JSON.parse(body || '{}'); } catch {}
        const outDir = path.join(ROOT, 'output');
        let manifest = o.manifest && fs.existsSync(path.join(ROOT, String(o.manifest))) ? path.join(ROOT, String(o.manifest)) : path.join(outDir, 'render_manifest.json');
        const resultFile = path.join(outDir, `.fb_result_${Date.now()}.json`);
        const logPath = path.join(outDir, `fb_upload_${Date.now()}.log`);
        const opts = JSON.stringify({ manifest, result: path.join(outDir, 'fb_upload_results.json'), updated: manifest + '_fb_uploaded.json', test_mode: !!o.test_mode });
        const out = fs.createWriteStream(logPath, { flags: 'a' });
        const child = spawn(PY, [path.join(__dirname, 'fb_upload.py'), resultFile], { env: { ...process.env, PYTHONIOENCODING: 'utf-8', FB_OPTS: opts } });
        child.stdout.pipe(out); child.stderr.pipe(out);
        FB_JOBS.set('run', { proc: child, code: undefined, startedAt: Date.now(), resultFile, logPath });
        child.on('close', code => { const j = FB_JOBS.get('run'); if (j) { j.code = code; j.finishedAt = Date.now(); } out.end(); });
        json(res, 200, { ok: true, started: true });
      });
      return;
    }
    // GET /api/fb/status
    if (p === '/api/fb/status') {
      const j = FB_JOBS.get('run');
      let result = null;
      try { if (j && j.code !== undefined && fs.existsSync(j.resultFile)) result = JSON.parse(fs.readFileSync(j.resultFile, 'utf8')); } catch {}
      return json(res, 200, {
        running: !!(j && j.code === undefined),
        code: j ? j.code : null,
        elapsed_s: j ? Math.round(((j.code !== undefined && j.finishedAt ? j.finishedAt : Date.now()) - j.startedAt) / 1000) : null,
        log: j && j.logPath ? tailFile(j.logPath) : '',
        result,
      });
    }
    // POST /api/fb/cancel
    if (p === '/api/fb/cancel' && req.method === 'POST') {
      const j = FB_JOBS.get('run');
      if (!j || j.code !== undefined) return json(res, 404, { error: 'Tidak ada upload berjalan' });
      try { process.kill(-j.proc.pid, 'SIGKILL'); } catch { try { j.proc.kill('SIGKILL'); } catch {} }
      return json(res, 200, { ok: true, cancelled: true });
    }
    // GET /api/story/sources | /api/story/recipe - baca JSON input (untuk editor UI)
    if (p === '/api/story/read' && req.method === 'GET') {
      const url = new URL(req.url, 'http://x');
      const file = String(url.searchParams.get('file') || '').replace(/[^a-z_]/gi, '');
      if (file !== 'sources' && file !== 'recipe') return json(res, 400, { error: 'file must be sources|recipe' });
      const fp = path.join(ROOT, 'output', 'story', file === 'sources' ? 'sources.json' : 'story_recipe.json');
      if (!fs.existsSync(fp)) return json(res, 200, { ok: true, content: null });
      try {
        return json(res, 200, { ok: true, content: fs.readFileSync(fp, 'utf8') });
      } catch (e) { return json(res, 500, { error: String(e) }); }
    }
    // POST /api/story/save {file:'sources'|'recipe', content} - simpan JSON input ke output/story
    if (p === '/api/story/save' && req.method === 'POST') {
      let body = ''; req.on('data', c => body += c); req.on('end', () => {
        let o = {}; try { o = JSON.parse(body || '{}'); } catch {}
        const file = String(o.file || '').replace(/[^a-z_]/gi, '');
        if (file !== 'sources' && file !== 'recipe') return json(res, 400, { error: 'file must be sources|recipe' });
        if (typeof o.content !== 'string' || !o.content.trim()) return json(res, 400, { error: 'content required' });
        if (file === 'sources') { try { const j = JSON.parse(o.content); if (typeof j !== 'object') throw 0; } catch { return json(res, 400, { error: 'sources.json bukan JSON valid' }); } }
        if (file === 'recipe') { try { const j = JSON.parse(o.content); if (typeof j !== 'object') throw 0; } catch { return json(res, 400, { error: 'story_recipe.json bukan JSON valid' }); } }
        try {
          const dir = path.join(ROOT, 'output', 'story');
          fs.mkdirSync(dir, { recursive: true });
          const fp = path.join(dir, file === 'sources' ? 'sources.json' : 'story_recipe.json');
          fs.writeFileSync(fp, o.content, 'utf8');
          return json(res, 200, { ok: true, path: fp });
        } catch (e) { return json(res, 500, { error: String(e) }); }
      });
      return;
    }
    // GET /api/fb/manifests - daftar render_manifest*.json di output/
    if (p === '/api/fb/manifests') {
      try {
        const outDir = path.join(ROOT, 'output');
        const files = fs.existsSync(outDir) ? fs.readdirSync(outDir).filter(f => /^render_manifest.*\.json$/.test(f)).sort() : [];
        return json(res, 200, files.map(f => ({ name: f, rel: path.join('output', f), abs: path.join(outDir, f), size: (() => { try { return fs.statSync(path.join(outDir, f)).size; } catch { return 0; } })() })));
      } catch (e) { return json(res, 500, { error: String(e) }); }
    }
    // POST /api/tasks/stop - hentikan job berjalan (SIGTERM → SIGKILL tree)
    if (p === '/api/tasks/stop' && req.method === 'POST') {
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        let o = {};
        try { o = JSON.parse(body || '{}'); } catch {}
        const kind = o.kind, qs = String(o.session || ''), qc = String(o.clip || '');
        let job = null, label = '';
        if (kind === 'create') { job = CREATE_JOB; label = 'find highlight'; }
        else if (kind === 'refind') { job = REFIND_JOBS.get(qs); label = `re-find ${qs}`; }
        else if (kind === 'process') { job = PROCESS_JOBS.get(qs); label = `process ${qs}`; }
        else if (kind === 'render') { job = RENDER_JOBS.get(`${qs}/${qc}`); label = `render ${qs}/${qc}`; }
        else if (kind === 'campaign') { 
          let campId = qs.startsWith('tk_') ? qs.slice(3) : qs;
          job = CAMPAIGN_JOBS.get(campId) || CAMPAIGN_JOBS.get(qs);
          if (!job) { for (const [cid, cj] of CAMPAIGN_JOBS) if (cj.session_id === qs) { job = cj; break; } }
          label = `campaign ${campId || qs}`;
        }
        else if (kind === 'story') { job = STORY_JOBS.get('run'); label = 'story clip'; }
        else if (kind === 'fb') { job = FB_JOBS.get('run'); label = 'facebook upload'; }
        if (!job) return json(res, 404, { error: 'job tidak ditemukan' });
        if (job.code !== undefined) return json(res, 409, { error: 'job sudah selesai' });
        try {
          const pid = job.proc.pid;
                    // bunuh subtree (python bisa spawn ffmpeg) - pkill di Unix, taskkill /T di Windows
                    if (isWin) {
                      try { execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' }); } catch {}
                    } else {
                      execFile('pkill', ['-TERM', '-P', String(pid)], () => {});
                    }
          try { process.kill(-pid, 'SIGTERM'); } catch { try { job.proc.kill('SIGTERM'); } catch {} }
          setTimeout(() => { try { process.kill(-pid, 'SIGKILL'); } catch { try { job.proc.kill('SIGKILL'); } catch {} } }, 4000);
        } catch {}
        json(res, 200, { ok: true, stopped: label });
      });
      return;
    }
    // GET /api/tasks - daftar semua job per sesi (buat halaman tasks)
    if (p === '/api/tasks') {
      const jobs = [];
      if (CREATE_JOB) {
        let createSid = '-';
        try { if (CREATE_JOB.resultFile && fs.existsSync(CREATE_JOB.resultFile)) { const r = JSON.parse(fs.readFileSync(CREATE_JOB.resultFile,'utf8')); if (r.session_id) createSid = r.session_id; } } catch {}
        // fallback: coba baca highlight count dari session_data jika ada
        if (createSid === '-' && CREATE_JOB.code === 0) {
          try { const outFiles = fs.readdirSync(path.join(ROOT,'output')).filter(f=>f.startsWith('.phase1_result_')).sort(); if(outFiles.length){ const last=JSON.parse(fs.readFileSync(path.join(ROOT,'output',outFiles[outFiles.length-1]),'utf8')); if(last.session_id) createSid=last.session_id; } } catch {}
        }
        jobs.push({ kind: 'create', type: '🔍 Find Highlight', session: createSid, detail: CREATE_JOB.url || '', running: CREATE_JOB.code === undefined, code: CREATE_JOB.code, elapsed_s: Math.round(((CREATE_JOB.code !== undefined && CREATE_JOB.finishedAt ? CREATE_JOB.finishedAt : Date.now()) - CREATE_JOB.startedAt) / 1000), logPath: CREATE_JOB.logPath });
      }
      for (const [sid, j] of REFIND_JOBS) jobs.push({ kind: 'refind', type: '🔁 Re-find', session: sid, detail: '', running: j.code === undefined, code: j.code, elapsed_s: Math.round(((j.code !== undefined && j.finishedAt ? j.finishedAt : Date.now()) - j.startedAt) / 1000), logPath: path.join(SESSIONS, sid, 'refind.log') });
      for (const [sid, j] of PROCESS_JOBS) jobs.push({ kind: 'process', type: '🎬 Process', session: sid, detail: '', running: j.code === undefined, code: j.code, elapsed_s: Math.round(((j.code !== undefined && j.finishedAt ? j.finishedAt : Date.now()) - j.startedAt) / 1000), logPath: path.join(SESSIONS, sid, 'process.log') });
      for (const [key, j] of RENDER_JOBS) {
        const [sid, clip] = key.split('/');
        jobs.push({ kind: 'render', type: '⚙️ Render', session: sid, detail: clip, running: j.code === undefined, code: j.code, elapsed_s: Math.round(((j.code !== undefined && j.finishedAt ? j.finishedAt : Date.now()) - j.startedAt) / 1000), logPath: path.join(SESSIONS, sid, 'clips', clip, 'render.log') });
      }
      for (const [campId, j] of CAMPAIGN_JOBS) jobs.push({ kind: 'campaign', type: '🚀 Campaign Auto', session: j.session_id || `tk_${campId}`, detail: `${campId} - ${j.stage} ${j.preset || ''}`.trim(), running: j.code === undefined, code: j.code, elapsed_s: Math.round(((j.code !== undefined && j.finishedAt ? j.finishedAt : Date.now()) - j.startedAt) / 1000), logPath: j.logPath });
      const storyJ = STORY_JOBS.get('run');
      if (storyJ) jobs.push({ kind: 'story', type: '🎬 Story Clip', session: '-', detail: '', running: storyJ.code === undefined, code: storyJ.code, elapsed_s: Math.round(((storyJ.code !== undefined && storyJ.finishedAt ? storyJ.finishedAt : Date.now()) - storyJ.startedAt) / 1000), logPath: storyJ.logPath });
      const fbJ = FB_JOBS.get('run');
      if (fbJ) jobs.push({ kind: 'fb', type: '📘 FB Upload', session: '-', detail: '', running: fbJ.code === undefined, code: fbJ.code, elapsed_s: Math.round(((fbJ.code !== undefined && fbJ.finishedAt ? fbJ.finishedAt : Date.now()) - fbJ.startedAt) / 1000), logPath: fbJ.logPath });
      for (const j of jobs) {
        try { j.last_line = lastLogLine(j.logPath); } catch { j.last_line = ''; }
        try { j.progress = parseOverall(tailFile(j.logPath)); } catch { j.progress = null; }
        delete j.logPath;
      }
      return json(res, 200, jobs);
    }
    // GET /api/tasks/log?kind=&session=&clip= - log/debug satu job
    if (p === '/api/tasks/log') {
      const kind = u.searchParams.get('kind');
      const qs = u.searchParams.get('session') || '';
      const qc = u.searchParams.get('clip') || '';
      let job = null, logPath = null;
      try {
        if (kind === 'create') { job = CREATE_JOB; logPath = job && job.logPath; }
        else if (kind === 'refind') { job = REFIND_JOBS.get(qs); logPath = qs ? path.join(SESSIONS, safe(qs), 'refind.log') : null; }
        else if (kind === 'process') { job = PROCESS_JOBS.get(qs); logPath = qs ? path.join(SESSIONS, safe(qs), 'process.log') : null; }
        else if (kind === 'render') { job = RENDER_JOBS.get(`${qs}/${qc}`); logPath = qs && qc ? path.join(SESSIONS, safe(qs), 'clips', safe(qc), 'render.log') : null; }
        else if (kind === 'campaign') { 
          // qs is campaign public_id (e.g. 4273c29e-...) or tk_xxx; support both
          let key = qs || (qc ? qc.split('/').pop() : '');
          // strip tk_ prefix if present
          let campId = key.startsWith('tk_') ? key.slice(3) : key;
          job = CAMPAIGN_JOBS.get(campId) || CAMPAIGN_JOBS.get(key);
          if (!job) {
            for (const [cid, cj] of CAMPAIGN_JOBS) if (cj.session_id === key) { job = cj; break; }
          }
          logPath = job && job.logPath;
        }
        else if (kind === 'story') { job = STORY_JOBS.get('run'); logPath = job && job.logPath; }
        else if (kind === 'fb') { job = FB_JOBS.get('run'); logPath = job && job.logPath; }
        else return json(res, 400, { error: 'bad kind' });
      } catch { return json(res, 400, { error: 'bad path' }); }
      if (!job) return json(res, 404, { error: 'job tidak ditemukan' });
      return json(res, 200, {
        running: job.code === undefined,
        code: job.code,
        elapsed_s: Math.round(((job.code !== undefined && job.finishedAt ? job.finishedAt : Date.now()) - job.startedAt) / 1000),
        log: logPath ? tailFile(logPath) : '',
      });
    }
    // GET /api/highlights/:session - daftar highlight utk picker
    const mHl = p.match(/^\/api\/highlights\/([^/]+)$/);
    if (mHl) {
      try {
        const data = JSON.parse(fs.readFileSync(path.join(SESSIONS, safe(mHl[1]), 'session_data.json'), 'utf8'));
        return json(res, 200, (data.highlights || []).map((h, i) => ({
          i, title: h.title, duration: h.duration_seconds, score: h.virality_score ?? null,
          reason: h.virality_reason || h.description || '',
          hook: h.hook_text || '', start: h.start_time, end: h.end_time,
        })));
      } catch { return json(res, 404, { error: 'not found' }); }
    }
    // POST /api/process/:session - phase 2: download section + render terpilih
    const mProc = p.match(/^\/api\/process\/([^/]+)$/);
    if (mProc && req.method === 'POST') {
      const sessDir = path.join(SESSIONS, safe(mProc[1]));
      if (!sessDir.startsWith(SESSIONS) || !fs.existsSync(path.join(sessDir, 'session_data.json'))) return json(res, 404, { error: 'session tidak ditemukan' });
      const prev = PROCESS_JOBS.get(mProc[1]);
      if (prev && prev.code === undefined) return json(res, 409, { error: 'Process untuk sesi ini masih berjalan' });
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        let o = {};
        try { o = JSON.parse(body || '{}'); } catch {}
        const sel = Array.isArray(o.selected) ? o.selected.filter(x => Number.isInteger(x)) : [];
        if (!sel.length) return json(res, 400, { error: 'Tidak ada highlight dipilih' });
        const env = { ...process.env,
                  SELECTED: sel.join(','),
                  ADD_HOOK: o.hook ? '1' : '0',
                  ADD_CAPS: o.captions ? '1' : '0',
                  BGM_MOOD: String(o.bgm_mood || ''),
                  BROLL_QUERY: String(o.broll_query || ''),
                };
        const logPath = path.join(sessDir, 'process.log');
        fs.appendFileSync(logPath, `\n===== process start ${new Date().toISOString()} sel=${env.SELECTED} hook=${env.ADD_HOOK} caps=${env.ADD_CAPS} (overall: 5.0%) =====\n`);
        const out = fs.createWriteStream(logPath, { flags: 'a' });
        const child = spawn(PY, [path.join(__dirname, 'process_session.py'), sessDir], { env, detached: true });
        child.stdout.pipe(out); child.stderr.pipe(out);
        const job = { proc: child, code: undefined, startedAt: Date.now() };
        PROCESS_JOBS.set(mProc[1], job);
        child.on('close', code => { job.code = code; job.finishedAt = Date.now(); out.end(); });
        json(res, 200, { ok: true, started: true });
      });
      return;
    }
    // GET /api/process/status/:session
    const mPst = p.match(/^\/api\/process\/status\/([^/]+)$/);
    if (mPst) {
      const sid = safe(mPst[1]);
      const job = PROCESS_JOBS.get(sid);
      return json(res, 200, {
        running: !!(job && job.code === undefined),
        code: job ? job.code : null,
        elapsed_s: job ? Math.round(((job.code !== undefined && job.finishedAt ? job.finishedAt : Date.now()) - job.startedAt) / 1000) : null,
        log: tailFile(path.join(SESSIONS, sid, 'process.log')),
        progress: parseOverall(tailFile(path.join(SESSIONS, sid, 'process.log'))),
      });
    }
    // POST /api/process/cancel/:session - hentikan render sesi berjalan
    const mPcx = p.match(/^\/api\/process\/cancel\/([^/]+)$/);
    if (mPcx && req.method === 'POST') {
      const job = PROCESS_JOBS.get(safe(mPcx[1]));
      if (!job || job.code !== undefined) return json(res, 404, { error: 'Tidak ada proses berjalan untuk sesi ini' });
      try { process.kill(-job.proc.pid, 'SIGKILL'); } catch { try { job.proc.kill('SIGKILL'); } catch {} }
      return json(res, 200, { ok: true, cancelled: true });
    }
    // /thumb/:session/:clipDir - frame @3s, cached ke thumb.jpg (opsional :file = file gambar asli di folder clip)
    const mThumb = p.match(/^\/thumb\/([^/]+)\/([^/]+)(?:\/([^/]+))?$/);
    if (mThumb) {
      const dir = path.join(SESSIONS, safe(mThumb[1]), 'clips', safe(mThumb[2]));
      if (!dir.startsWith(SESSIONS)) return json(res, 403, { error: 'forbidden' });
      const fname = mThumb[3] ? path.basename(mThumb[3]) : null;
      const directImg = fname ? path.join(dir, fname) : '';
      if (fname && fs.existsSync(directImg) && fs.statSync(directImg).isFile() && /\.(png|jpe?g)$/i.test(fname)) {
        res.writeHead(200, { 'Content-Type': /\.png$/i.test(fname) ? 'image/png' : 'image/jpeg', 'Cache-Control': 'max-age=86400' });
        return fs.createReadStream(directImg).pipe(res);
      }
      const out = path.join(dir, 'thumb.jpg');
      const serve = () => { res.writeHead(200, { 'Content-Type': 'image/jpeg', 'Cache-Control': 'max-age=86400' }); fs.createReadStream(out).pipe(res); };
      if (fs.existsSync(out)) return serve();
      let src = null;
      try { const mt = JSON.parse(fs.readFileSync(path.join(dir, 'data.json'))); const t = `${mt.title || ''}.mp4`; if (t && fs.existsSync(path.join(dir, t))) src = t; } catch {}
      if (!src) src = VARIANTS.find(v => fs.existsSync(path.join(dir, v))) || fs.readdirSync(dir).find(f => f.toLowerCase().endsWith('.mp4'));
      if (!src) return json(res, 404, { error: 'no video' });
      const tmp = path.join(dir, 'thumb.tmp.jpg');
      return execFile(FFMPEG, ['-y', '-ss', '3', '-i', path.join(dir, src), '-frames:v', '1', '-vf', 'scale=360:-2', '-q:v', '5', tmp], err => {
        try { if (!err && fs.existsSync(tmp)) fs.renameSync(tmp, out); else fs.existsSync(tmp) && fs.unlinkSync(tmp); } catch {}
        if (err || !fs.existsSync(out)) return json(res, 500, { error: 'ffmpeg failed' });
        serve();
      });
    }
    // inject widget disk ke sidebar semua halaman HTML (tanpa merubah file HTML)
    let fp = path.join(PUBLIC, p === '/' ? 'index.html' : p);
    if ((p === '/' || /\.html$/.test(p)) && !p.includes('login.html')) {
      getDiskStats().then(st => {
        if (!fs.existsSync(fp) || !fs.statSync(fp).isFile()) return json(res, 404, { error: 'not found' });
        let html = fs.readFileSync(fp, 'utf8');
        const widget = st.error ? '' : [
          '<div class="disk-widget px-4 py-3 border-t border-zinc-800/80 text-xs text-zinc-400 bg-zinc-950/40">',
          '  <div class="flex items-center justify-between mb-1.5">',
          '    <span class="font-semibold text-[10px] uppercase tracking-wider text-zinc-400">💾 Disk</span>',
          `    <span class="text-zinc-400" id="diskLabel">${fmtGB(st.used)} / ${fmtGB(st.total)}</span>`,
          '  </div>',
          `  <div class="h-2 w-full bg-zinc-800 rounded-full overflow-hidden" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${st.usedPercent}" aria-label="Penggunaan disk">`,
          `    <div class="h-full rounded-full transition-all duration-500 ${diskBarColor(st.usedPercent)}" style="width:${Math.min(100, st.usedPercent)}%"></div>`,
          '  </div>',
          '</div>'
        ].join('\n');
        if (widget) html = injectDisk(html, widget);
        html = injectDiskScript(html);
        res.writeHead(200, {
          'Content-Type': MIME['.html'],
          'Cache-Control': 'no-cache',
        });
        res.end(html);
      });
      return;
    }
    if (!fp.startsWith(PUBLIC)) return json(res, 403, { error: 'forbidden' });
    if (!fs.existsSync(fp) || !fs.statSync(fp).isFile()) return json(res, 404, { error: 'not found' });
    // static fallback: login.html & semua aset non-HTML (css/js/img) tanpa inject
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(fp)] || 'application/octet-stream',
      'Cache-Control': fp.endsWith('.html') ? 'no-cache' : 'max-age=86400',
    });
    res.end(fs.readFileSync(fp));
  } catch (e) {
    json(res, e.message === 'bad path' ? 400 : 500, { error: e.message });
  }
});

server.listen(PORT, () => console.log(`http://localhost:${PORT}  (auth: password)`));
