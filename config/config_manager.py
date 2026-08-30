"""
Configuration manager for Auto Clipper
"""

import json
import uuid
from pathlib import Path


class ConfigManager:
    """Manages application configuration"""
    
    def __init__(self, config_file: Path, output_dir: Path):
        self.config_file = config_file
        self.output_dir = output_dir
        self.config = self.load()
    
    def load(self):
        """Load configuration from file"""
        if self.config_file.exists():
            with open(self.config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
                
                # Migrate old config to new multi-provider structure
                if "api_key" in config and "ai_providers" not in config:
                    config = self._migrate_to_multi_provider(config)
                
                # Add default system_prompt if not exists
                if "system_prompt" not in config:
                    from clipper_core import AutoClipperCore
                    config["system_prompt"] = AutoClipperCore.get_default_prompt()
                # Add default temperature if not exists
                if "temperature" not in config:
                    config["temperature"] = 1.0
                # Add default tts_model if not exists (for backward compatibility)
                if "tts_model" not in config:
                    config["tts_model"] = "tts-1"
                # Add default watermark settings if not exists
                if "watermark" not in config:
                    config["watermark"] = {
                        "enabled": False,
                        "image_path": "",
                        "position_x": 0.85,  # 0-1 (percentage from left)
                        "position_y": 0.05,  # 0-1 (percentage from top)
                        "opacity": 0.8,      # 0-1
                        "scale": 0.15        # 0-1 (percentage of video width)
                    }
                # Add default face tracking mode if not exists
                if "face_tracking_mode" not in config:
                    config["face_tracking_mode"] = "mediapipe"
                # migrate old opencv/detector → mediapipe (hapus opencv biar ga ribet)
                if config.get("face_tracking_mode") in ("opencv", "detector", "center"):
                    config["face_tracking_mode"] = "mediapipe"
                    self.save_config(config)
                # Add default portrait mode if not exists
                if "portrait_mode" not in config:
                    config["portrait_mode"] = "crop"  # "crop" | "blur" | "split"|"split_game"|"split_podcast"|"split_podcast_dynamic"
                # Add default subtitle style if not exists
                if "subtitle_style" not in config:
                    config["subtitle_style"] = "pop"  # "pop" (word pop highlight), "karaoke", "bounce", or "animated"
                # Add default aspect ratio if not exists
                if "aspect_ratio" not in config:
                    config["aspect_ratio"] = "9:16"  # "9:16", "1:1", "4:5", or "16:9"
                # Add default MediaPipe settings if not exists — tuned for speaker-accurate (not center)
                if "mediapipe_settings" not in config:
                    config["mediapipe_settings"] = {
                        "lip_activity_threshold": 0.08,
                        "switch_threshold": 0.18,
                        "min_shot_duration": 45,
                        "center_weight": 0.15,
                        "smooth_follow": False,
                        "pan_speed_limit": 1.8,
                    }
                # Generate installation_id if not exists
                if "installation_id" not in config:
                    config["installation_id"] = str(uuid.uuid4())
                    self.save_config(config)
                
                # Ensure ai_providers structure exists
                if "ai_providers" not in config:
                    config["ai_providers"] = self._get_default_ai_providers()
                    self.save_config(config)
                
                # Add default Repliz settings if not exists
                if "repliz" not in config:
                    config["repliz"] = {
                        "access_key": "",
                        "secret_key": ""
                    }
                
                # Add default GPU settings if not exists
                if "gpu_acceleration" not in config:
                    config["gpu_acceleration"] = {
                        "enabled": False
                    }
                
                config = self._ensure_new_feature_defaults(config)
                
                return config
        
        # Default config with system prompt
        from clipper_core import AutoClipperCore
        config = {
            "api_key": "",  # Kept for backward compatibility
            "base_url": "https://api.openai.com/v1",  # Kept for backward compatibility
            "model": "gpt-4.1",  # Kept for backward compatibility
            "tts_model": "tts-1",  # Kept for backward compatibility
            "temperature": 1.0,
            "output_dir": str(self.output_dir),
            "system_prompt": AutoClipperCore.get_default_prompt(),
            "installation_id": str(uuid.uuid4()),
            "ai_providers": self._get_default_ai_providers(),
            "watermark": {
                "enabled": False,
                "image_path": "",
                "position_x": 0.85,
                "position_y": 0.05,
                "opacity": 0.8,
                "scale": 0.15
            },
            "face_tracking_mode": "opencv",
            "portrait_mode": "crop",
            "subtitle_style": "pop",
            "aspect_ratio": "9:16",
            "mediapipe_settings": {
                "lip_activity_threshold": 0.08,
                "switch_threshold": 0.18,
                "min_shot_duration": 45,
                "center_weight": 0.15,
                "smooth_follow": False,
                "pan_speed_limit": 1.8
            },
            "repliz": {
                "access_key": "",
                "secret_key": ""
            },
            "gpu_acceleration": {
                "enabled": False
            }
        }
        config = self._ensure_new_feature_defaults(config)
        self.save_config(config)
        return config
    
    def _ensure_new_feature_defaults(self, config):
        """Isi default untuk fitur baru (opensource-clipping adaptations) —
        tanpa menimpa nilai yang sudah diatur user."""
        wm = config.setdefault("watermark", {})
        wm.setdefault("position", "")         # ""=pakai position_x/y, atau 0-8 / tl..br
        wm.setdefault("padding", 0.02)
        wm.setdefault("text", "")

        config.setdefault("font_preset", "DEFAULT")  # typography (feature 8)

        config.setdefault("auto_bgm", {
            "enabled": False,
            "mood": "",
            "mode": "ducking",                # "ducking" | "background"
            "base_volume": 0.25,
            "bgm_dir": "",
            "path": "",
        })

        config.setdefault("auto_broll", {
            "enabled": False,
            "pexels_api_key": "",
            "per_clip": 1,
            "duration": 3.0,
            "mix_volume": 0.35,
        })

        config.setdefault("transition_library", {
            "enabled": False,
            "type": "slide_up",
            "duration": 0.5,
        })

        config.setdefault("auto_camera_switch", {
            "enabled": False,
            "hold_duration": 2.0,
            "blend_duration": 0.0,
            "deadzone": 0.15,
            "smooth": 0.30,
            "max_zoom": 3.0,
        })

        config.setdefault("face_detector_model", "mediapipe")  # "mediapipe" | "yolo"
        config.setdefault("yolo_size", "8n")

        config.setdefault("thumbnail", {
            "enabled": False,
            "text": "",
            "render_front": True,
            "position": "bottom",
            "font_size": 0.05,
        })

        config.setdefault("metadata_settings", {
            "classification": "auto",
            "target_platforms": ["youtube", "tiktok", "facebook"],
            "save_preview": True,
        })

        config.setdefault("story_clip", {
            "enabled": False,
            "ratio": "9:16",
            "whisper_model": "medium",
            "download_source_height": "max",
        })

        config.setdefault("facebook_uploader", {
            "enabled": False,
            "page_id": "",
            "access_token": "",
            "graph_version": "v25.0",
            "tz_name": "Asia/Makassar",
            "interval_hours": 5,
            "test_mode": False,
        })

        ps = config.setdefault("pro_settings", {})
        ps.setdefault("camera_switch_step", 0.25)
        ps.setdefault("camera_switch_deadzone", 0.15)
        ps.setdefault("camera_switch_smooth", 0.30)
        ps.setdefault("switch_hold_duration", 2.0)
        ps.setdefault("switch_blend_duration", 0.0)
        ps.setdefault("camera_switch_max_zoom", 3.0)
        return config

    def _get_default_ai_providers(self):
        """Get default AI provider configuration"""
        return {
            "highlight_finder": {
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "gpt-4.1"
            },
            "caption_maker": {
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "whisper-1",
                "faster_whisper": {
                    "mode": "api",
                    "model_size": "small"
                }
            },
            "hook_maker": {
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "tts-1"
            },
            "youtube_title_maker": {
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "gpt-4.1"
            }
        }
    
    def _migrate_to_multi_provider(self, old_config):
        """Migrate old single-provider config to new multi-provider structure"""
        api_key = old_config.get("api_key", "")
        base_url = old_config.get("base_url", "https://api.openai.com/v1")
        model = old_config.get("model", "gpt-4.1")
        tts_model = old_config.get("tts_model", "tts-1")
        
        old_config["ai_providers"] = {
            "highlight_finder": {
                "base_url": base_url,
                "api_key": api_key,
                "model": model
            },
            "caption_maker": {
                "base_url": base_url,
                "api_key": api_key,
                "model": "whisper-1"
            },
            "hook_maker": {
                "base_url": base_url,
                "api_key": api_key,
                "model": tts_model
            },
            "youtube_title_maker": {
                "base_url": base_url,
                "api_key": api_key,
                "model": model
            }
        }
        
        return old_config

    def save(self):
        """Save configuration to file"""
        self.save_config(self.config)
    
    def save_config(self, config):
        """Save configuration dict to file"""
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    
    def get(self, key, default=None):
        """Get configuration value"""
        return self.config.get(key, default)
    
    def set(self, key, value):
        """Set configuration value and save"""
        self.config[key] = value
        self.save()
