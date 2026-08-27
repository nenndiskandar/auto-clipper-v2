// yt-short-clipper result viewer — zero deps, node >= 16
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
  const base = { 'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes' };
  if (download) base['Content-Disposition'] = `attachment; filename="${path.basename(fp).replace(/"/g, '')}"`;
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

const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  const p = u.pathname;
  try {
    // --- gate auth Telegram ---
    if (p === '/api/auth/telegram' && req.method === 'POST') {
      let body = '';
      req.on('data', c => body += c);
      req.on('end', () => {
        let d = {};
        try { d = JSON.parse(body || '{}'); } catch {}
        const check = Object.keys(d).filter(k => k !== 'hash').sort().map(k => `${k}=${d[k]}`).join('\n');
        let okHash = false;
        try {
          const secret = crypto.createHash('sha256').update(BOT_TOKEN).digest();
          const hmac = crypto.createHmac('sha256', secret).update(check).digest('hex');
          const a = Buffer.from(hmac), b = Buffer.from(String(d.hash || ''));
          okHash = a.length === b.length && crypto.timingSafeEqual(a, b);
        } catch {}
        const fresh = Math.abs(Date.now() / 1000 - Number(d.auth_date)) < 86400;
        if (!BOT_TOKEN || !okHash || !fresh || String(d.id) !== OWNER_ID) return json(res, 403, { error: 'Verifikasi Telegram gagal' });
        res.setHeader('Set-Cookie', `${COOKIE_NAME}=${makeToken(d.id, d.first_name || d.username || 'admin')}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${7 * 86400}`);
        json(res, 200, { ok: true, name: d.first_name || d.username || 'admin' });
      });
      return;
    }
    if (p === '/logout') {
      res.setHeader('Set-Cookie', `${COOKIE_NAME}=; Path=/; HttpOnly; Max-Age=0`);
      res.writeHead(302, { Location: '/' });
      return res.end();
    }
    // --- public routes: video stream & download (tanpa auth) ---
    const mVidPub = p.match(/^\/(video|download)\/([^/]+)\/(.+)$/);
    if (mVidPub) {
      const parts = mVidPub[3].split('/').map(safe);
      if (parts.length !== 2) return json(res, 400, { error: 'bad path' });
      const fp = path.join(SESSIONS, safe(mVidPub[2]), 'clips', parts[0], parts[1]);
      if (!fp.startsWith(SESSIONS)) return json(res, 403, { error: 'forbidden' });
      return sendFile(req, res, fp, mVidPub[1] === 'download');
    }
    // --- thumbnail juga publik ---
    const mThPub = p.match(/^\/thumb\/([^/]+)\/([^/]+)$/);
    if (mThPub) {
      const clipDir = path.join(SESSIONS, safe(mThPub[1]), 'clips', safe(mThPub[2]));
      if (!clipDir.startsWith(SESSIONS)) return json(res, 403, { error: 'forbidden' });
      const thumbFiles = ['thumbnail.jpg','thumbnail.png','thumb.jpg','thumb.png'];
      let found = null;
      for (const t of thumbFiles) { const tp = path.join(clipDir, t); if (fs.existsSync(tp)) { found = tp; break; } }
      if (!found) {
        const mp4s = (fs.existsSync(clipDir) ? fs.readdirSync(clipDir) : []).filter(f => f.endsWith('.mp4'));
        if (!mp4s.length) return json(res, 404, { error: 'no thumb' });
      }
      if (found) return sendFile(req, res, found, false);
      return json(res, 404, { error: 'no thumb' });
    }

    if (!checkToken(getCookie(req.headers.cookie)[COOKIE_NAME])) {
      if (p === '/' || p.endsWith('.html')) {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-cache' });
        return res.end(fs.readFileSync(path.join(PUBLIC, 'login.html')));
      }
      return json(res, 401, { error: 'unauthorized' });
    }
    if (p === '/api/me') {
      const me = checkToken(getCookie(req.headers.cookie)[COOKIE_NAME]);
      return json(res, 200, me ? { id: me.id, name: me.name } : {});
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
          if (!o.name || typeof o.name !== 'string' || !o.cfg) return json(res, 400, { error: 'invalid preset format' });
          try {
            fs.mkdirSync(path.dirname(pFile), { recursive: true });
            let current = {};
            try { if (fs.existsSync(pFile)) current = JSON.parse(fs.readFileSync(pFile, 'utf8')); } catch {}
            if (o.delete) {
              delete current[o.name];
            } else {
              current[o.name] = { label: o.label || o.name, desc: o.desc || 'Custom preset', cfg: o.cfg };
            }
            fs.writeFileSync(pFile, JSON.stringify(current, null, 2), 'utf8');
            return json(res, 200, { ok: true, presets: current });
          } catch (e) { return json(res, 500, { error: String(e) }); }
        });
        return;
      }
    }
    // /video|download/:session/:clipDir/:file
    const mVid = p.match(/^\/(video|download)\/([^/]+)\/(.+)$/);
    if (mVid) {
      const parts = mVid[3].split('/').map(safe);
      if (parts.length !== 2) return json(res, 400, { error: 'bad path' });
      const fp = path.join(SESSIONS, safe(mVid[2]), 'clips', parts[0], parts[1]);
      if (!fp.startsWith(SESSIONS)) return json(res, 403, { error: 'forbidden' });
      return sendFile(req, res, fp, mVid[1] === 'download');
    }
    // POST /api/delete/:session/:clipDir — hapus folder klip (trash bila ada, fallback rm)
    const mDel = p.match(/^\/api\/delete\/([^/]+)\/([^/]+)$/);
    if (mDel && req.method === 'POST') {
      const dir = path.join(SESSIONS, safe(mDel[1]), 'clips', safe(mDel[2]));
      if (!dir.startsWith(SESSIONS) || !fs.existsSync(dir)) return json(res, 404, { error: 'not found' });
      invalidateSessions();
      execFile('trash', [dir], err => {
        if (err) fs.rmSync(dir, { recursive: true, force: true });
        json(res, 200, { ok: true, method: err ? 'rm' : 'trash' });
      });
      return;
    }
    // POST /api/delete-session/:session — hapus seluruh folder sesi (trash bila ada, fallback rm)
    const mDelS = p.match(/^\/api\/delete-session\/([^/]+)$/);
    if (mDelS && req.method === 'POST') {
      const dir = path.join(SESSIONS, safe(mDelS[1]));
      if (!dir.startsWith(SESSIONS) || !fs.existsSync(dir)) return json(res, 404, { error: 'not found' });
      invalidateSessions();
      execFile('trash', [dir], err => {
        if (err) fs.rmSync(dir, { recursive: true, force: true });
        json(res, 200, { ok: true, method: err ? 'rm' : 'trash' });
      });
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
          smooth_follow: mp.smooth_follow !== false,
          pan_speed_limit: mp.pan_speed_limit ?? 2.5,
          center_weight: mp.center_weight ?? 0.3,
          switch_threshold: mp.switch_threshold ?? 0.3,
          min_shot_duration: mp.min_shot_duration ?? 90,
          lip_activity: mp.lip_activity_threshold ?? 0.15,
          gpu: !!(cfg.gpu_acceleration && cfg.gpu_acceleration.enabled),
          hf_model: (ap.highlight_finder || {}).model || 'AUTO',
          server_url: (ap.highlight_finder || {}).base_url || '',
          fw_model: fwSize,
          fw_installed: fwInstalled,
          wm: cfg.watermark || {},
          cw: cfg.credit_watermark || {},
          hookstyle: cfg.hook_style || {},
          core_model: cfg.model || 'gpt-4.1',
          tts_model: cfg.tts_model || 'tts-1',
          temperature: cfg.temperature ?? 1.0,
          subtitle_language: cfg.subtitle_language || 'id',
          hf_system_message: ((ap.highlight_finder || {}).system_message) || '',
          hf_api_key_set: !!((ap.highlight_finder || {}).api_key),
          // Pro video editing features
          pro_settings: cfg.pro_settings || {},
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
        cfg.ai_providers = cfg.ai_providers || {};
        cfg.ai_providers.highlight_finder = cfg.ai_providers.highlight_finder || {};
        if (typeof o.hf_model === 'string' && o.hf_model.trim()) cfg.ai_providers.highlight_finder.model = o.hf_model.trim();
        if (typeof o.server_url === 'string' && o.server_url.trim()) cfg.ai_providers.highlight_finder.base_url = o.server_url.trim();
        if (typeof o.fw_model === 'string' && o.fw_model.trim()) {
          cfg.ai_providers.caption_maker = cfg.ai_providers.caption_maker || {};
          cfg.ai_providers.caption_maker.faster_whisper = Object.assign({}, cfg.ai_providers.caption_maker.faster_whisper, { model_size: o.fw_model.trim() });
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
        if (o.hookstyle && typeof o.hookstyle === 'object') {
          cfg.hook_style = Object.assign({}, cfg.hook_style);
          for (const k of ['font_size', 'corner_radius', 'position_x', 'position_y']) if (isNum(o.hookstyle[k])) cfg.hook_style[k] = o.hookstyle[k];
          for (const k of ['font_color', 'bg_color']) if (typeof o.hookstyle[k] === 'string' && /^#[0-9a-fA-F]{6}$/.test(o.hookstyle[k])) cfg.hook_style[k] = o.hookstyle[k];
          if ('glitch' in o.hookstyle) cfg.hook_style.glitch = !!o.hookstyle.glitch;
        }
        if (isNum(o.temperature)) cfg.temperature = Math.min(2, Math.max(0, o.temperature));
        if (typeof o.core_model === 'string' && o.core_model.trim()) cfg.model = o.core_model.trim();
        if (typeof o.tts_model === 'string' && o.tts_model.trim()) cfg.tts_model = o.tts_model.trim();
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
if (p === '/api/test-connection' && req.method === 'POST') {
  let body = '';
  req.on('data', c => body += c);
  req.on('end', () => {
    let o = {};
    try { o = JSON.parse(body || '{}'); } catch {}
    const env = { ...process.env, TC_URL: String(o.server_url || '').trim(), TC_KEY: String(o.api_key || '') };
    execFile('/usr/bin/python3', ['-c', TC_SRC], { env }, (err, stdout, stderr) => {
      const out = (stdout || '').toString().trim().split('\n').pop();
      try { return json(res, 200, JSON.parse(out)); } catch { return json(res, 200, { ok: false, error: (stderr || stdout || '').toString().slice(-300) }); }
    });
  });
  return;
}
// GET /api/binaries — status dependensi
    if (p === '/api/binaries') {
      const bin = [
        { name: 'ffmpeg', ok: fs.existsSync(path.join(ROOT, 'ffmpeg')), detail: path.join(ROOT, 'ffmpeg') + ' (bundled)' },
        { name: 'deno', ok: fs.existsSync(path.join(ROOT, 'bin', 'deno')), detail: path.join(ROOT, 'bin', 'deno') + ' (bundled)' },
      ];
      // ponytail: versi yt-dlp dicek tiap request (~300ms); cache kalau jadi bottleneck
      execFile('python3', ['-c', 'import yt_dlp;print(yt_dlp.version.__version__)'], (err, stdout) => {
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
        const child = spawn('/usr/bin/python3', [path.join(__dirname, 'render_clip.py'), sessDir, clipDir], { env });
        child.stdout.pipe(out);
        child.stderr.pipe(out);
        const job = { proc: child, code: undefined, startedAt: Date.now() };
        RENDER_JOBS.set(key, job);
        child.on('close', code => { job.code = code; out.end(); });
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
        elapsed_s: job ? Math.round((Date.now() - job.startedAt) / 1000) : null,
        log,
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
        const child = spawn('/usr/bin/python3', [path.join(__dirname, 'phase1_create.py'), String(o.url), String(parseInt(o.num_clips) || 0), resultFile], { detached: true });
        child.stdout.pipe(out); child.stderr.pipe(out);
        CREATE_JOB = { proc: child, code: undefined, startedAt: Date.now(), resultFile, logPath, url: String(o.url) };
        child.on('close', code => { CREATE_JOB.code = code; out.end(); });
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
        elapsed_s: j ? Math.round((Date.now() - j.startedAt) / 1000) : null,
        url: j ? j.url : null,
        log: j && j.logPath ? tailFile(j.logPath) : '',
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
        const child = spawn('/usr/bin/python3', [path.join(__dirname, 'refind_highlights.py'), sid, String(parseInt(o.num_clips) || 0), resultFile], { detached: true });
        child.stdout.pipe(out); child.stderr.pipe(out);
        const job = { proc: child, code: undefined, startedAt: Date.now(), resultFile };
        REFIND_JOBS.set(sid, job);
        child.on('close', code => { job.code = code; out.end(); });
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
        elapsed_s: job ? Math.round((Date.now() - job.startedAt) / 1000) : null,
        log: tailFile(path.join(SESSIONS, sid, 'refind.log'), 6000),
        result,
      });
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
        if (!job) return json(res, 404, { error: 'job tidak ditemukan' });
        if (job.code !== undefined) return json(res, 409, { error: 'job sudah selesai' });
        try {
          const pid = job.proc.pid;
          // bunuh subtree (python bisa spawn ffmpeg)
          execFile('pkill', ['-TERM', '-P', String(pid)], () => {});
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
      if (CREATE_JOB) jobs.push({ kind: 'create', type: '🔍 Find Highlight', session: '-', detail: CREATE_JOB.url || '', running: CREATE_JOB.code === undefined, code: CREATE_JOB.code, elapsed_s: Math.round((Date.now() - CREATE_JOB.startedAt) / 1000), logPath: CREATE_JOB.logPath });
      for (const [sid, j] of REFIND_JOBS) jobs.push({ kind: 'refind', type: '🔁 Re-find', session: sid, detail: '', running: j.code === undefined, code: j.code, elapsed_s: Math.round((Date.now() - j.startedAt) / 1000), logPath: path.join(SESSIONS, sid, 'refind.log') });
      for (const [sid, j] of PROCESS_JOBS) jobs.push({ kind: 'process', type: '🎬 Process', session: sid, detail: '', running: j.code === undefined, code: j.code, elapsed_s: Math.round((Date.now() - j.startedAt) / 1000), logPath: path.join(SESSIONS, sid, 'process.log') });
      for (const [key, j] of RENDER_JOBS) {
        const [sid, clip] = key.split('/');
        jobs.push({ kind: 'render', type: '⚙️ Render', session: sid, detail: clip, running: j.code === undefined, code: j.code, elapsed_s: Math.round((Date.now() - j.startedAt) / 1000), logPath: path.join(SESSIONS, sid, 'clips', clip, 'render.log') });
      }
      for (const j of jobs) {
        try { j.last_line = lastLogLine(j.logPath); } catch { j.last_line = ''; }
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
        else return json(res, 400, { error: 'bad kind' });
      } catch { return json(res, 400, { error: 'bad path' }); }
      if (!job) return json(res, 404, { error: 'job tidak ditemukan' });
      return json(res, 200, {
        running: job.code === undefined,
        code: job.code,
        elapsed_s: Math.round((Date.now() - job.startedAt) / 1000),
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
        };
        const logPath = path.join(sessDir, 'process.log');
        fs.appendFileSync(logPath, `\n===== process start ${new Date().toISOString()} sel=${env.SELECTED} hook=${env.ADD_HOOK} caps=${env.ADD_CAPS} =====\n`);
        const out = fs.createWriteStream(logPath, { flags: 'a' });
        const child = spawn('/usr/bin/python3', [path.join(__dirname, 'process_session.py'), sessDir], { env, detached: true });
        child.stdout.pipe(out); child.stderr.pipe(out);
        const job = { proc: child, code: undefined, startedAt: Date.now() };
        PROCESS_JOBS.set(mProc[1], job);
        child.on('close', code => { job.code = code; out.end(); });
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
        elapsed_s: job ? Math.round((Date.now() - job.startedAt) / 1000) : null,
        log: tailFile(path.join(SESSIONS, sid, 'process.log')),
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
    // /thumb/:session/:clipDir — frame @3s, cached ke thumb.jpg
    const mThumb = p.match(/^\/thumb\/([^/]+)\/([^/]+)$/);
    if (mThumb) {
      const dir = path.join(SESSIONS, safe(mThumb[1]), 'clips', safe(mThumb[2]));
      if (!dir.startsWith(SESSIONS)) return json(res, 403, { error: 'forbidden' });
      const out = path.join(dir, 'thumb.jpg');
      const serve = () => { res.writeHead(200, { 'Content-Type': 'image/jpeg', 'Cache-Control': 'max-age=86400' }); fs.createReadStream(out).pipe(res); };
      if (fs.existsSync(out)) return serve();
      let src = null;
      try { const mt = JSON.parse(fs.readFileSync(path.join(dir, 'data.json'))); const t = `${mt.title || ''}.mp4`; if (t && fs.existsSync(path.join(dir, t))) src = t; } catch {}
      if (!src) src = VARIANTS.find(v => fs.existsSync(path.join(dir, v))) || fs.readdirSync(dir).find(f => f.toLowerCase().endsWith('.mp4'));
      if (!src) return json(res, 404, { error: 'no video' });
      const tmp = path.join(dir, 'thumb.tmp.jpg');
      return execFile('ffmpeg', ['-y', '-ss', '3', '-i', path.join(dir, src), '-frames:v', '1', '-vf', 'scale=360:-2', '-q:v', '5', tmp], err => {
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

server.listen(PORT, () => console.log(`http://localhost:${PORT}`));
