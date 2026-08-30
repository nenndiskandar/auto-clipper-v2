// Shared presets across all pages (Create, Session, Settings)
// Dioptimasi untuk fitur saat ini: BGM mood, B-roll (Pexels), camera-switch,
// transition library, thumbnail, font preset, YOLO face detector.
// auto_bgm/auto_broll/auto_camera_switch boleh object {enabled, mood/query/...}
// (server merge; create.html cukup pakai .enabled-nya).
window.UNIFIED_TEMPLATES = {
  tiktok_viral: {
    label: '🔥 TikTok Viral Thumbnail',
    desc: '9:16, subtitle pop bounce, hook glitch, BGM upbeat + B-roll hype, thumbnail auto',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop_bounce',
      captions: true,
      hook: true,
      gpu: true,
      hook_style: { font_color: '#ffffff', bg_color: '#000000', corner_radius: 12, font_size: 0.08, glitch: true },
      smooth_follow: false,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.5,
      center_weight: 0.6,
      switch_threshold: 0.35,
      min_shot_duration: 30,
      lip_activity: 0.05,
      color_grade: 'cinematic',
      stabilize: false,
      sync_offset: 0,
      auto_bgm: { enabled: true, mood: 'upbeat', mode: 'ducking', base_volume: 0.25 },
      auto_broll: { enabled: true, query: 'viral hype cinematic' },
      transition_library: 'cut',
      thumbnail: { enabled: true },
      face_detector_model: 'mediapipe'
    }
  },
  podcast_dynamic: {
    label: '🎙️ Podcast Dynamic Split (OpusClip)',
    desc: 'Split 2 orang, camera-switch otomatis, BGM chill, thumbnail auto',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'split_podcast_dynamic',
      subtitle_style: 'karaoke',
      captions: true,
      hook: true,
      gpu: true,
      hook_style: { font_color: '#ffd166', bg_color: '#000000', corner_radius: 8, font_size: 0.08, glitch: false },
      smooth_follow: false,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.5,
      center_weight: 0.12,
      switch_threshold: 0.18,
      min_shot_duration: 45,
      lip_activity: 0.08,
      color_grade: 'warm',
      stabilize: false,
      sync_offset: -0.25,
      auto_bgm: { enabled: true, mood: 'chill', mode: 'ducking', base_volume: 0.2 },
      auto_camera_switch: { enabled: true, hold_duration: 2.0, deadzone: 0.15, smooth: 0.3, max_zoom: 3.0 },
      thumbnail: { enabled: true },
      face_detector_model: 'mediapipe'
    }
  },
  face_center_rapih: {
    label: '⭐ Wajah Tengah Rapih',
    desc: 'Center face follow, BGM chill ringan, thumbnail auto',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'center',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      gpu: true,
      hook_style: { font_color: '#ffffff', bg_color: '#000000', corner_radius: 12, font_size: 0.075, glitch: false },
      smooth_follow: false,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.6,
      center_weight: 0.10,
      switch_threshold: 0.18,
      min_shot_duration: 45,
      lip_activity: 0.08,
      color_grade: 'warm',
      stabilize: false,
      sync_offset: -0.25,
      auto_bgm: { enabled: true, mood: 'chill', mode: 'background', base_volume: 0.15 },
      thumbnail: { enabled: true }
    }
  },
  gaming_action: {
    label: '🎮 Gaming / Action',
    desc: 'Subtitle bounce, warna cool, BGM epic, transition random, thumbnail auto',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'bounce',
      captions: true,
      hook: true,
      gpu: true,
      hook_style: { font_color: '#25f4ee', bg_color: '#111118', corner_radius: 10, font_size: 0.08, glitch: true },
      smooth_follow: false,
      face_tracking_mode: 'opencv',
      pan_speed_limit: 3.0,
      center_weight: 0.15,
      switch_threshold: 0.15,
      min_shot_duration: 20,
      lip_activity: 0.2,
      color_grade: 'cool',
      motion_blur: 1.5,
      stabilize: false,
      sync_offset: -0.15,
      auto_bgm: { enabled: true, mood: 'epic', mode: 'ducking', base_volume: 0.3 },
      transition_library: 'random',
      thumbnail: { enabled: true }
    }
  },
  education_clean: {
    label: '📚 Edukasi / Tutorial',
    desc: 'Bersih, thumbnail teks tegas, B-roll whiteboard, BGM subtle',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      gpu: true,
      hook_style: { font_color: '#ffffff', bg_color: '#fe2c55', corner_radius: 8, font_size: 0.075, glitch: false },
      smooth_follow: false,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.0,
      center_weight: 0.20,
      switch_threshold: 0.30,
      min_shot_duration: 60,
      lip_activity: 0.08,
      color_grade: 'none',
      stabilize: false,
      sync_offset: -0.3,
      auto_bgm: { enabled: true, mood: 'chill', mode: 'background', base_volume: 0.15 },
      auto_broll: { enabled: true, query: 'whiteboard tutorial writing' },
      thumbnail: { enabled: true }
    }
  },
  news_formal: {
    label: '📰 Berita / Formal',
    desc: 'Kamera statis, subtitle karaoke, warna netral, thumbnail auto',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'karaoke',
      captions: true,
      hook: false,
      gpu: true,
      smooth_follow: false,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.3,
      center_weight: 0.15,
      switch_threshold: 0.30,
      min_shot_duration: 60,
      lip_activity: 0.08,
      color_grade: 'none',
      stabilize: false,
      sync_offset: -0.3,
      thumbnail: { enabled: true }
    }
  },
  vlog_dynamic: {
    label: '📹 Vlog Dinamis',
    desc: 'Follow kamera halus, BGM upbeat, B-roll travel, transisi crossfade',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop_bounce',
      captions: true,
      hook: true,
      gpu: true,
      hook_style: { font_color: '#ffffff', bg_color: '#000000', corner_radius: 12, font_size: 0.075, glitch: false },
      smooth_follow: false,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.8,
      center_weight: 0.4,
      switch_threshold: 0.35,
      min_shot_duration: 45,
      lip_activity: 0.12,
      color_grade: 'warm',
      vignette: 0.2,
      stabilize: false,
      sync_offset: -0.25,
      auto_bgm: { enabled: true, mood: 'upbeat', mode: 'ducking', base_volume: 0.25 },
      auto_broll: { enabled: true, query: 'travel cinematic vlog' },
      transition_library: 'crossfade',
      thumbnail: { enabled: true }
    }
  },
  square_feed: {
    label: '⏹️ IG / FB Feed (1:1)',
    desc: 'Rasio kotak 1:1, karaoke tengah, BGM upbeat, thumbnail auto',
    cfg: {
      aspect_ratio: '1:1',
      portrait_mode: 'crop',
      subtitle_style: 'karaoke',
      captions: true,
      hook: true,
      gpu: true,
      hook_style: { font_color: '#ffe600', bg_color: '#141414', corner_radius: 6, font_size: 0.08, glitch: false },
      auto_bgm: { enabled: true, mood: 'upbeat', mode: 'background', base_volume: 0.2 },
      thumbnail: { enabled: true }
    }
  },
  reels_34: {
    label: '🎬 Reels FB/IG (3:4)',
    desc: 'Rasio 3:4 untuk Reels/FB, B-roll + transisi crossfade, thumbnail auto',
    cfg: {
      aspect_ratio: '3:4',
      portrait_mode: 'crop',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      gpu: true,
      hook_style: { font_color: '#ffffff', bg_color: '#0064e0', corner_radius: 10, font_size: 0.08, glitch: false },
      smooth_follow: false,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.5,
      center_weight: 0.3,
      switch_threshold: 0.25,
      min_shot_duration: 45,
      lip_activity: 0.1,
      sync_offset: -0.2,
      auto_bgm: { enabled: true, mood: 'upbeat', mode: 'ducking', base_volume: 0.25 },
      auto_broll: { enabled: true, query: 'lifestyle aesthetic' },
      transition_library: 'crossfade',
      thumbnail: { enabled: true }
    }
  },
  story_time: {
    label: '📖 Story Time Naratif',
    desc: 'B-roll naratif + BGM suspense, transisi crossfade, thumbnail teks',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      gpu: true,
      hook_style: { font_color: '#ffffff', bg_color: '#7c3aed', corner_radius: 12, font_size: 0.08, glitch: false },
      smooth_follow: false,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.4,
      center_weight: 0.25,
      switch_threshold: 0.25,
      min_shot_duration: 45,
      lip_activity: 0.1,
      color_grade: 'cinematic',
      sync_offset: -0.25,
      auto_bgm: { enabled: true, mood: 'suspense', mode: 'ducking', base_volume: 0.2 },
      auto_broll: { enabled: true, query: 'mystery dark cinematic' },
      transition_library: 'crossfade',
      thumbnail: { enabled: true }
    }
  }
};

// Auto-reload shared presets: poll templates.js and fire onChange when it changes.
window.TemplatesAPI = {
  poll(intervalMs, onChange) {
    intervalMs = intervalMs || 5000;
    let lastHash = null;
    const hash = s => { let h = 0; for (let i = 0; i < s.length; i++) { h = (h << 5) - h + s.charCodeAt(i); h |= 0; } return h; };
    async function tick() {
      try {
        const txt = await (await fetch('/templates.js?ts=' + Date.now(), { cache: 'no-store' })).text();
        const h = hash(txt);
        if (h !== lastHash) {
          lastHash = h;
          new Function(txt)();                 // re-assigns window.UNIFIED_TEMPLATES
          if (typeof onChange === 'function') onChange(window.UNIFIED_TEMPLATES || {});
        }
      } catch (e) { /* ignore transient errors */ }
    }
    tick();
    setInterval(tick, intervalMs);
  }
};