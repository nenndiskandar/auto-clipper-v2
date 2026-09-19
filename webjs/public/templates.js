// Shared presets across all pages (Create, Session, Settings)
// Dioptimasi untuk fitur aktif: Auto Follow Face (crop/blur),
// Subtitle Typography (Pop/Karaoke), Hook Overlay, Cover Thumbnail.
window.UNIFIED_TEMPLATES = {
  tiktok_viral: {
    label: '🔥 TikTok Viral Thumbnail',
    desc: '9:16, subtitle pop, hook glitch, thumbnail auto',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      gpu: true,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.5,
      center_weight: 0.6,
      switch_threshold: 0.35,
      min_shot_duration: 30,
      lip_activity: 0.05,
      sync_offset: 0,
      thumbnail: { enabled: true }
    }
  },
  gaming_action: {
    label: '🎮 Gaming / Action',
    desc: 'Subtitle pop, hook glitch, thumbnail auto',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      gpu: true,
      hook_style: { box_mode: 'fit_text', font_color: '#25f4ee', bg_color: '#111118', corner_radius: 10, font_size: 0.08, bg_opacity: 100, glitch: true },
      smooth_follow: false,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 3.0,
      center_weight: 0.15,
      switch_threshold: 0.15,
      min_shot_duration: 20,
      lip_activity: 0.2,
      sync_offset: -0.15,
      thumbnail: { enabled: true }
    }
  },
  education_clean: {
    label: '📚 Edukasi / Tutorial',
    desc: 'Bersih, subtitle pop, thumbnail auto',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      gpu: true,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.0,
      center_weight: 0.20,
      switch_threshold: 0.30,
      min_shot_duration: 60,
      lip_activity: 0.08,
      sync_offset: -0.3,
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
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.3,
      center_weight: 0.15,
      switch_threshold: 0.30,
      min_shot_duration: 60,
      lip_activity: 0.08,
      sync_offset: -0.3,
      thumbnail: { enabled: true }
    }
  },
  vlog_dynamic: {
    label: '📹 Vlog Dinamis',
    desc: 'Follow kamera halus, subtitle pop, thumbnail auto',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      gpu: true,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.8,
      center_weight: 0.4,
      switch_threshold: 0.35,
      min_shot_duration: 45,
      lip_activity: 0.12,
      sync_offset: -0.25,
      thumbnail: { enabled: true }
    }
  },
  square_feed: {
    label: '⏹️ IG / FB Feed (1:1)',
    desc: 'Rasio kotak 1:1, karaoke tengah, thumbnail auto',
    cfg: {
      aspect_ratio: '1:1',
      portrait_mode: 'crop',
      subtitle_style: 'karaoke',
      captions: true,
      hook: true,
      gpu: true,
      hook_style: { box_mode: 'fit_text', font_color: '#ffe600', bg_color: '#141414', corner_radius: 6, font_size: 0.08, bg_opacity: 100, glitch: false },
      face_detector_model: 'mediapipe',
      sync_offset: 0,
      thumbnail: { enabled: true }
    }
  },
  reels_34: {
    label: '🎬 Reels FB/IG (3:4)',
    desc: 'Rasio 3:4 untuk Reels/FB, subtitle pop, thumbnail auto',
    cfg: {
      aspect_ratio: '3:4',
      portrait_mode: 'crop',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      gpu: true,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.5,
      center_weight: 0.3,
      switch_threshold: 0.25,
      min_shot_duration: 45,
      lip_activity: 0.1,
      sync_offset: -0.2,
      thumbnail: { enabled: true }
    }
  },
  story_time: {
    label: '📖 Story Time Naratif',
    desc: 'Preset naratif, subtitle pop, thumbnail teks',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      gpu: true,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.4,
      center_weight: 0.25,
      switch_threshold: 0.25,
      min_shot_duration: 45,
      lip_activity: 0.1,
      sync_offset: -0.25,
      thumbnail: { enabled: true }
    }
  }
};

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
          new Function(txt)();
          if (typeof onChange === 'function') onChange(window.UNIFIED_TEMPLATES || {});
        }
      } catch (e) { }
    }
    tick();
    setInterval(tick, intervalMs);
  }
};
