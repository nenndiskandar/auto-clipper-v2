// Shared presets across all pages (Create, Session, Settings)
window.UNIFIED_TEMPLATES = {
  tiktok_viral: {
    label: '🔥 TikTok Viral Hook',
    desc: '9:16 portrait, subtitle pop bounce, hook TikTok glitch, dynamic camera follow',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop_bounce',
      captions: true,
      hook: true,
      hook_style: { font_color: '#ffffff', bg_color: '#000000', corner_radius: 12, font_size: 0.08, glitch: true },
      smooth_follow: true,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.5,
      center_weight: 0.6,
      switch_threshold: 0.35,
      min_shot_duration: 2.0,
      lip_activity: 0.05,
      color_grade: 'cinematic',
      stabilize: true,
      sync_offset: 0
    }
  },
  podcast_reels: {
    label: '🎙️ Podcast / Talkshow',
    desc: 'Face tracking mediapipe, subtitle karaoke aktif, crop fokus speaker',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'karaoke',
      captions: true,
      hook: true,
      hook_style: { font_color: '#ffd166', bg_color: '#ffffff', corner_radius: 0, font_size: 0.075, glitch: false },
      smooth_follow: true,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.2,
      center_weight: 0.7,
      switch_threshold: 0.3,
      min_shot_duration: 2.5,
      lip_activity: 0.06,
      color_grade: 'warm',
      stabilize: false,
      sync_offset: -0.25
    }
  },
  podcast_blur: {
    label: '🎙️ Podcast 3+ (Blur)',
    desc: 'Banyak orang/host, blur background, kamera responsif cepat',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'blur',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      hook_style: { font_color: '#ffffff', bg_color: '#fe2c55', corner_radius: 10, font_size: 0.08, glitch: false },
      smooth_follow: false,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 2.0,
      center_weight: 0.3,
      switch_threshold: 0.25,
      min_shot_duration: 1.5,
      lip_activity: 0.12,
      color_grade: 'warm',
      stabilize: false,
      sync_offset: -0.25
    }
  },
  gaming_action: {
    label: '🎮 Gaming / Action',
    desc: 'Subtitle bounce energetik, warna cool/tajam, gerak kamera sangat cepat',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'bounce',
      captions: true,
      hook: true,
      hook_style: { font_color: '#25f4ee', bg_color: '#111118', corner_radius: 10, font_size: 0.08, glitch: true },
      smooth_follow: true,
      face_tracking_mode: 'opencv',
      pan_speed_limit: 2.5,
      center_weight: 0.15,
      switch_threshold: 0.15,
      min_shot_duration: 0.8,
      lip_activity: 0.2,
      color_grade: 'cool',
      motion_blur: 1.5,
      stabilize: false,
      sync_offset: -0.15
    }
  },
  education_clean: {
    label: '📚 Edukasi / Tutorial',
    desc: 'Tampilan bersih, subtitle pop jelas, hook merah tegas, kamera ultra-stabil',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop',
      captions: true,
      hook: true,
      hook_style: { font_color: '#ffffff', bg_color: '#fe2c55', corner_radius: 8, font_size: 0.075, glitch: false },
      smooth_follow: true,
      face_tracking_mode: 'opencv',
      pan_speed_limit: 0.8,
      center_weight: 0.8,
      switch_threshold: 0.45,
      min_shot_duration: 3.0,
      lip_activity: 0.08,
      color_grade: 'none',
      stabilize: true,
      sync_offset: -0.3
    }
  },
  news_formal: {
    label: '📰 Berita / Formal',
    desc: 'Kamera statis stabil, subtitle karaoke, warna netral',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'karaoke',
      captions: true,
      hook: false,
      smooth_follow: false,
      face_tracking_mode: 'opencv',
      pan_speed_limit: 1.0,
      center_weight: 0.6,
      switch_threshold: 0.45,
      min_shot_duration: 3.0,
      lip_activity: 0.08,
      color_grade: 'none',
      stabilize: true,
      sync_offset: -0.3
    }
  },
  cinematic_blur: {
    label: '🎬 Sinematik Blur',
    desc: 'Background blur, subtitle animated cyan, color grade cinematic, vignette halus',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'blur',
      subtitle_style: 'animated',
      captions: true,
      hook: false,
      color_grade: 'cinematic',
      vignette: 0.25,
      stabilize: true,
      sync_offset: 0
    }
  },
  vlog_dynamic: {
    label: '📹 Vlog Dinamis',
    desc: 'Follow kamera halus, subtitle pop bounce, warna warm aesthetic',
    cfg: {
      aspect_ratio: '9:16',
      portrait_mode: 'crop',
      subtitle_style: 'pop_bounce',
      captions: true,
      hook: true,
      hook_style: { font_color: '#ffffff', bg_color: '#000000', corner_radius: 12, font_size: 0.075, glitch: false },
      smooth_follow: true,
      face_tracking_mode: 'mediapipe',
      pan_speed_limit: 1.8,
      center_weight: 0.4,
      switch_threshold: 0.35,
      min_shot_duration: 1.8,
      lip_activity: 0.12,
      color_grade: 'warm',
      vignette: 0.2,
      stabilize: false,
      sync_offset: -0.25
    }
  },
  square_feed: {
    label: '⏹️ IG / FB Feed (1:1)',
    desc: 'Rasio kotak 1:1, subtitle karaoke tengah untuk feed Instagram/Facebook',
    cfg: {
      aspect_ratio: '1:1',
      portrait_mode: 'crop',
      subtitle_style: 'karaoke',
      captions: true,
      hook: true,
      hook_style: { font_color: '#ffe600', bg_color: '#141414', corner_radius: 6, font_size: 0.08, glitch: false }
    }
  }
};
