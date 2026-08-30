// auto-clipper result viewer — zero deps, node >= 16
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
  const cands = isWin ? ['py', 'python', 'python3'] : ['/usr/bin/python3', 'python3', 'python'];
  for (const c of cands) {
    try {
      execFileSync(c, ['-c', 'import cv2, sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'], { stdio: 'ignore' });
      return c;
    } catch {}
  }
  return cands[0]; // fallback: let the script surface the real error
}
const PY = resolvePy();

// Resolve bundled/system ffmpeg: <ROOT>/ffmpeg/ffmpeg[.exe] first, then PATH, else 'ffmpeg'.
const FFMPEG = (() => {
  const bundled = path.join(ROOT, 'ffmpeg', isWin ? 'ffmpeg.exe' : 'ffmpeg');
  if (fs.existsSync(bundled)) return bundled;
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

// --- Auth: Telegram Login Widget (cookie HMAC, total pengganti basic-auth) ---
const OWNER_ID = '233439175';
const BOT_USERNAME = 'iskanclip_bot';
const COOKIE_NAME = 'clipper_auth';
const BOT_TOKEN = (() => { try { return JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json'), 'utf8')).telegram_bot_token || ''; } catch { return ''; } })();
const AUTH_KEY = crypto.createHash('sha256').update('clipper-web:' + BOT_TOKEN).digest();

const signVal = v => crypto.createHmac('sha256', AUTH_KEY).update(v).digest('base64url');
const b64u = s => Buffer.from(String(s), 'utf8').toString('base64url');
const makeToken = (id, name) => { const exp = Date.now() + 7 * 864e5; const nb = b64u(name || ''); return `${id}|${exp}|${nb}|${signVal(id + '|' + exp + '|' + nb)}`; };
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

// final-output preference order inside each clip folder
const VARIANTS = ['credit.mp4', 'watermark.mp4', 'captioned.mp4', 'portrait.mp4'];

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml' };

// job render aktif: key "session/clipDir" -> { proc, code, startedAt }
const RENDER_JOBS = new Map();
// job create (phase1) & process (phase2)
let CREATE_JOB = null;
const PROCESS_JOBS = new Map();
const REFIND_JOBS = new Map();
// job story clip & facebook upload
let STORY_JOBS = new Map(); // key "run" -> job (biar /api/tasks legible)
const FB_JOBS = new Map();  // key "run" -> job

function safe(seg) {
  if (!seg || seg.includes('..') || seg.includes('/') || seg.includes('\\')) throw new Error('bad path');
  return decodeURIComponent(seg);
}

function json(res, code, obj) {
  res.writeHead(code, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(obj));
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

function _listSessions() {
  return fs.readdirSync(SESSIONS)
    .filter(d => fs.existsSync(path.join(SESSIONS, d, 'session_data.json')))
    .map(d => {
      try {
        const sd = path.join(SESSIONS, d);
        const data = JSON.parse(fs.readFileSync(path.join(sd, 'session_data.json')));
        const hlCount = Array.isArray(data.highlights) ? data.highlights.length : 0;
        return {
          id: d,
          url: data.url || null,
          status: data.status || 'unknown',
          title: (data.video_info && data.video_info.title) || d,
          channel: (data.video_info && data.video_info.channel) || '',
          created: fs.statSync(sd).mtime,
          total: listClips(sd, data.highlights).length,
          total_highlights: hlCount,
          clips: listClips(sd, data.highlights),
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
    if (p === '/api/auth/telegram' && req.method === 'POST') {
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        try {
          const user = JSON.parse(body || '{}');
          if (String(user.id) === OWNER_ID) {
            const token = makeToken(user.id, user.first_name);
            res.writeHead(200, { 'Set-Cookie': `${COOKIE_NAME}=${token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800` });
            return json(res, 200, { ok: true });
          }
          json(res, 403, { error: 'ID tidak diizinkan' });
        } catch { json(res, 400, { error: 'invalid request' }); }
      });
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
    // /video/story/:clip/:file — stream hasil Story Clip (output/story_clips)
    const mVidStory = p.match(/^\/video\/story\/([^/]+)\/(.+)$/);
    if (mVidStory) {
      const clipDir = path.join(ROOT, 'output', 'story_clips', safe(mVidStory[1]));
      const file = path.basename(mVidStory[2]);
      const fp = path.join(clipDir, file);
      if (!clipDir.startsWith(ROOT) || !fs.existsSync(fp) || !fs.statSync(fp).isFile()) { res.writeHead(404, { 'Content-Type': 'video/mp4' }); return res.end(); }
      return sendFile(req, res, fp, false);
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
    // GET /api/sessions/:session/clip/:clipDir — detail 1 klip (ringan, tanpa scan semua sesi)
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
    // GET/POST /api/sessions/:session/subtitle — ambil & simpan editan subtitle SRT
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
    // GET/POST /api/presets — simpan & ambil custom presets
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
    // POST /api/delete/:session/:clipDir — hapus folder klip (trash bila ada, fallback rm)
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
    // POST /api/delete-session/:session — hapus seluruh folder sesi (trash bila ada, fallback rm)
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
    // GET /api/config — konfigurasi aktif (satu sumber dengan bot /config)
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
          hf_api_key_set: !!((ap.highlight_finder || {}).api_key),
          // Pro video editing features
          pro_settings: cfg.pro_settings || {},
          // Fitur baru (feature 1-19)
          face_detector_model: cfg.face_detector_model || 'mediapipe',
          yolo_size: cfg.yolo_size || '8n',
          font_preset: cfg.font_preset || 'DEFAULT',
          auto_bgm: cfg.auto_bgm || {},
          auto_broll: cfg.auto_broll || {},
          auto_camera_switch: cfg.auto_camera_switch || {},
          transition_library: cfg.transition_library || {},
          thumbnail: cfg.thumbnail || {},
          metadata_settings: cfg.metadata_settings || {},
          story_clip: cfg.story_clip || {},
          facebook_uploader: cfg.facebook_uploader || {},
          cs: {
            step: (cfg.pro_settings && cfg.pro_settings.camera_switch_step) ?? 0.25,
            deadzone: (cfg.pro_settings && cfg.pro_settings.camera_switch_deadzone),
            smooth: (cfg.pro_settings && cfg.pro_settings.camera_switch_smooth),
            hold_duration: (cfg.pro_settings && cfg.pro_settings.switch_hold_duration),
            blend_duration: (cfg.pro_settings && cfg.pro_settings.switch_blend_duration),
            max_zoom: (cfg.pro_settings && cfg.pro_settings.camera_switch_max_zoom),
          },
        });
      } catch { return json(res, 500, { error: 'config.json tidak terbaca' }); }
    }
    // POST /api/config — simpan perubahan parameter (merge; key lain tidak disentuh)
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
        if (typeof o.subtitle_style === 'string' && o.subtitle_style) cfg.subtitle_style = o.subtitle_style;
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
        // Fitur baru (feature 1-19)
        cfg.auto_camera_switch = cfg.auto_camera_switch || {};
        if ('acs_enabled' in o) cfg.auto_camera_switch.enabled = !!o.acs_enabled;
        for (const [k, sk] of [['acs_deadzone','deadzone'],['acs_smooth','smooth'],['acs_hold','hold_duration'],['acs_blend','blend_duration'],['acs_max_zoom','max_zoom']]) {
          if (isNum(o[k])) cfg.auto_camera_switch[sk] = o[k];
        }
        for (const [k, sk] of [['cs_step','camera_switch_step'],['cs_deadzone','camera_switch_deadzone'],['cs_smooth','camera_switch_smooth'],['cs_hold','switch_hold_duration'],['cs_blend','switch_blend_duration'],['cs_max_zoom','camera_switch_max_zoom']]) {
          if (isNum(o[k])) cfg.pro_settings[sk] = o[k];
        }
        if (typeof o.face_detector_model === 'string' && o.face_detector_model.trim()) cfg.face_detector_model = o.face_detector_model.trim();
        if (typeof o.yolo_size === 'string' && o.yolo_size.trim()) cfg.yolo_size = o.yolo_size.trim();
        if (typeof o.font_preset === 'string' && o.font_preset.trim()) cfg.font_preset = o.font_preset.trim();
        if (typeof o.wm === 'object' && o.wm) {
          if (typeof o.wm.position === 'string') cfg.watermark.position = o.wm.position;
          if (typeof o.wm.text === 'string') cfg.watermark.text = o.wm.text;
          if (isNum(o.wm.padding)) cfg.watermark.padding = o.wm.padding;
        }
        if (o.auto_bgm && typeof o.auto_bgm === 'object') {
          cfg.auto_bgm = Object.assign({}, cfg.auto_bgm, o.auto_bgm);
          if ('enabled' in o.auto_bgm) cfg.auto_bgm.enabled = !!o.auto_bgm.enabled;
        }
        if (o.thumbnail && typeof o.thumbnail === 'object') {
          cfg.thumbnail = Object.assign({}, cfg.thumbnail, o.thumbnail);
          if ('enabled' in o.thumbnail) cfg.thumbnail.enabled = !!o.thumbnail.enabled;
        }
        // Normalisasi input UI (boolean/string sederhana) ke bentuk object dict yang dipakai engine
        cfg.auto_bgm = cfg.auto_bgm || {};
        if (typeof o.auto_bgm === 'boolean') cfg.auto_bgm.enabled = o.auto_bgm;
        cfg.auto_broll = cfg.auto_broll || {};
        if (typeof o.auto_broll === 'boolean') cfg.auto_broll.enabled = o.auto_broll;
        if (typeof o.auto_broll === 'string') cfg.auto_broll.enabled = o.auto_broll !== 'none' && o.auto_broll !== 'false' && o.auto_broll !== '';
        cfg.transition_library = cfg.transition_library || {};
        if (typeof o.transition_library === 'string') {
          cfg.transition_library.enabled = o.transition_library.trim() !== 'none' && o.transition_library.trim() !== '';
          if (['random', 'cut', 'crossfade', 'static'].includes(o.transition_library.trim())) cfg.transition_library.style = o.transition_library.trim();
        }
        cfg.auto_camera_switch = cfg.auto_camera_switch || {};
        if (typeof o.auto_camera_switch === 'boolean') cfg.auto_camera_switch.enabled = o.auto_camera_switch;
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
    // POST /api/test-connection — cek reachable + valid API key (list models, OpenAI-compatible)
const TC_SRC = `import os, json
from openai import OpenAI
u=os.environ.get('TC_URL','').strip()
k=os.environ.get('TC_KEY','').strip()
try:
    c=OpenAI(api_key=k or 'x', base_url=u or None)
    m=c.models.list()
    ids=[getattr(x,'id',str(x)) for x in m.data][:20]
    print(json.dumps({'ok':True,'count':len(ids),'sample':ids}))
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
    const env = { ...process.env, TC_URL: String(o.server_url || '').trim(), TC_KEY: String(o.api_key || o.hf_api_key || '') };
    execFile(PY, ['-c', TC_SRC], { env }, (err, stdout, stderr) => {
      const out = (stdout || '').toString().trim().split('\n').pop();
      try { return json(res, 200, JSON.parse(out)); } catch { return json(res, 200, { ok: false, error: (stderr || stdout || '').toString().slice(-300) }); }
    });
  });
  return;
}
// GET /api/whisper/models — cek semua faster-whisper model (installed/ size)
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
    // POST /api/whisper/download {size} — download model async
    if (p === '/api/whisper/download' && req.method === 'POST') {
      let body=''; req.on('data',c=>body+=c); req.on('end',()=>{
        let o={}; try{o=JSON.parse(body||'{}')}catch{}
        const size=String(o.size||'').trim();
        if(!['tiny','base','small','medium','large-v3'].includes(size)) return json(res,400,{error:'invalid size'});
        const logPath=path.join(ROOT,'output',`whisper_download_${size}.log`);
        const out=fs.createWriteStream(logPath,{flags:'a'});
        fs.appendFileSync(logPath,`\n===== download ${size} ${new Date().toISOString()} =====\n`);
        const child=spawn(PY, ['-c', `from pathlib import Path;from utils.dependency_manager import setup_faster_whisper_model;import sys;ok=setup_faster_whisper_model(Path(r'${ROOT.replace(/\\/g,'\\\\')}'), '${size}');print('DONE:'+str(ok));sys.exit(0 if ok else 1)`], {env:{...process.env, PYTHONIOENCODING:'utf-8'}});
        child.stdout.pipe(out); child.stderr.pipe(out);
        child.on('close',code=>{ out.end(); });
        json(res,200,{ok:true, started:true, log: logPath});
      });
      return;
    }
    // GET /api/binaries — status dependensi
    if (p === '/api/binaries') {
          // Cross-platform binary probe: bundled first (with .exe on Windows), then PATH.
          const findBin = (name, rel) => {
            const probe = isWin ? [rel + '.exe', rel] : [rel];
            for (const p of probe) if (fs.existsSync(p)) return { ok: true, detail: p + ' (bundled)' };
            for (const dir of (process.env.PATH || '').split(path.delimiter)) {
              for (const p of probe) {
                const full = path.join(dir, path.basename(p));
                if (fs.existsSync(full)) return { ok: true, detail: full + ' (PATH)' };
              }
            }
            return { ok: false, detail: 'tidak terdeteksi' };
          };
          const bin = [
            { name: 'ffmpeg', ...findBin('ffmpeg', path.join(ROOT, 'ffmpeg', 'ffmpeg')) },
            { name: 'deno', ...findBin('deno', path.join(ROOT, 'bin', 'deno')) },
          ];
      // ponytail: versi yt-dlp dicek tiap request (~300ms); cache kalau jadi bottleneck
      execFile(PY, ['-c', 'import yt_dlp;print(yt_dlp.version.__version__)'], (err, stdout) => {
        bin.push({ name: 'yt-dlp', ok: !err, detail: err ? 'tidak terdeteksi' : 'v' + stdout.trim() + ' (module)' });
        json(res, 200, bin);
      });
      return;
    }
    // POST /api/render/:session/:clipDir — re-render klip dgn config aktif (async + log)
    const mRen = p.match(/^\/api\/render\/([^/]+)\/([^/]+)$/);
    if (mRen && req.method === 'POST') {
      const sessDir = path.join(SESSIONS, safe(mRen[1]));
      const clipDir = path.join(sessDir, 'clips', safe(mRen[2]));
      if (!clipDir.startsWith(SESSIONS) || !fs.existsSync(clipDir)) return json(res, 404, { error: 'not found' });
      if (!fs.existsSync(path.join(clipDir, 'landscape.mp4'))) return json(res, 400, { error: 'landscape.mp4 tidak ada — sumber render hilang' });
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
    // GET /api/render/status/:session/:clipDir — status + tail log
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
    // POST /api/create — phase 1: subtitle + AI highlights (seperti bot)
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
    // POST /api/create/cancel — hentikan analisis berjalan
    if (p === '/api/create/cancel' && req.method === 'POST') {
      const j = CREATE_JOB;
      if (!j || j.code !== undefined) return json(res, 404, { error: 'Tidak ada analisis berjalan' });
      try { process.kill(-j.proc.pid, 'SIGKILL'); } catch { try { j.proc.kill('SIGKILL'); } catch {} }
      return json(res, 200, { ok: true, cancelled: true });
    }
    // POST /api/upload/watermark — simpan gambar watermark ke assets/watermarks + set config
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
    // POST /api/refind/:session — regenerate highlights sesi ada (async + log)
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
    // GET /api/dashboard — ringkasan agregat: total klip, viral tertinggi, story outputs, klip terbaru
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
    // POST /api/story/run — jalankan Story Clip pipeline (multi-source) async
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
    // GET /api/story/outputs — daftar hasil dari output/story_clips
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
    // POST /api/fb/upload — jalankan Facebook Reels uploader async
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
    // GET /api/story/sources | /api/story/recipe — baca JSON input (untuk editor UI)
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
    // POST /api/story/save {file:'sources'|'recipe', content} — simpan JSON input ke output/story
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
    // GET /api/fb/manifests — daftar render_manifest*.json di output/
    if (p === '/api/fb/manifests') {
      try {
        const outDir = path.join(ROOT, 'output');
        const files = fs.existsSync(outDir) ? fs.readdirSync(outDir).filter(f => /^render_manifest.*\.json$/.test(f)).sort() : [];
        return json(res, 200, files.map(f => ({ name: f, rel: path.join('output', f), abs: path.join(outDir, f), size: (() => { try { return fs.statSync(path.join(outDir, f)).size; } catch { return 0; } })() })));
      } catch (e) { return json(res, 500, { error: String(e) }); }
    }
    // POST /api/tasks/stop — hentikan job berjalan (SIGTERM → SIGKILL tree)
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
        else if (kind === 'story') { job = STORY_JOBS.get('run'); label = 'story clip'; }
        else if (kind === 'fb') { job = FB_JOBS.get('run'); label = 'facebook upload'; }
        if (!job) return json(res, 404, { error: 'job tidak ditemukan' });
        if (job.code !== undefined) return json(res, 409, { error: 'job sudah selesai' });
        try {
          const pid = job.proc.pid;
                    // bunuh subtree (python bisa spawn ffmpeg) — pkill di Unix, taskkill /T di Windows
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
    // GET /api/tasks — daftar semua job per sesi (buat halaman tasks)
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
    // GET /api/tasks/log?kind=&session=&clip= — log/debug satu job
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
    // GET /api/highlights/:session — daftar highlight utk picker
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
    // POST /api/process/:session — phase 2: download section + render terpilih
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
    // POST /api/process/cancel/:session — hentikan render sesi berjalan
    const mPcx = p.match(/^\/api\/process\/cancel\/([^/]+)$/);
    if (mPcx && req.method === 'POST') {
      const job = PROCESS_JOBS.get(safe(mPcx[1]));
      if (!job || job.code !== undefined) return json(res, 404, { error: 'Tidak ada proses berjalan untuk sesi ini' });
      try { process.kill(-job.proc.pid, 'SIGKILL'); } catch { try { job.proc.kill('SIGKILL'); } catch {} }
      return json(res, 200, { ok: true, cancelled: true });
    }
    // /thumb/:session/:clipDir — frame @3s, cached ke thumb.jpg (opsional :file = file gambar asli di folder clip)
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
    // static
    let fp = path.join(PUBLIC, p === '/' ? 'index.html' : p);
    if (!fp.startsWith(PUBLIC)) return json(res, 403, { error: 'forbidden' });
    if (!fs.existsSync(fp) || !fs.statSync(fp).isFile()) return json(res, 404, { error: 'not found' });
    // ponytail: HTML selalu revalidate biar update UI langsung kelihatan (browser/Telegram in-app suka nge-cache)
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(fp)] || 'application/octet-stream',
      'Cache-Control': fp.endsWith('.html') ? 'no-cache' : 'max-age=86400',
    });
    res.end(fs.readFileSync(fp));
  } catch (e) {
    json(res, e.message === 'bad path' ? 400 : 500, { error: e.message });
  }
});

server.listen(PORT, () => console.log(`http://localhost:${PORT}  (auth: telegram bot, owner ${OWNER_ID})`));
