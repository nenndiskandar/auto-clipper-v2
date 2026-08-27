import os
import subprocess

class EffectsMixin:
    def apply_color_grade(self, input_path: str, output_path: str, style: str = "cinematic"):
        return self.apply_color_grade_with_progress(input_path, output_path, lambda p: None, style)

    def apply_color_grade_with_progress(self, input_path: str, output_path: str, progress_callback, style: str = "cinematic"):
        # Color grading filter map
        filters = {
            "cinematic": "eq=contrast=1.1:brightness=-0.05:saturation=1.2,curves=preset=strong_contrast",
            "warm": "colorbalance=rm=0.1:gm=0.05:bm=-0.1,eq=saturation=1.1",
            "cool": "colorbalance=rm=-0.1:gm=0.0:bm=0.15,eq=saturation=1.05",
            "vibrant": "eq=saturation=1.3:contrast=1.15",
            "noir": "format=gray,eq=contrast=1.3:brightness=-0.1"
        }
        vf = filters.get(style, filters["cinematic"])
        encoder_args = self.get_video_encoder_args()
        cmd = [self.ffmpeg_path, "-y", "-i", input_path, "-vf", vf, *encoder_args, "-c:a", "copy", output_path]
        self.log_ffmpeg_command(cmd, f"Color Grade ({style})", step="fx")
        duration = self._get_duration(input_path)
        self.run_ffmpeg_with_progress(cmd, duration, progress_callback)

    def apply_motion_blur(self, input_path: str, output_path: str, strength: int = 3):
        return self.apply_motion_blur_with_progress(input_path, output_path, lambda p: None, strength)

    def apply_motion_blur_with_progress(self, input_path: str, output_path: str, progress_callback, strength: int = 3):
        vf = f"minterpolate=mi_mode=mci:mc_mode=obmc:vsbmc=1:fps=60"
        encoder_args = self.get_video_encoder_args()
        cmd = [self.ffmpeg_path, "-y", "-i", input_path, "-vf", vf, *encoder_args, "-c:a", "copy", output_path]
        self.log_ffmpeg_command(cmd, f"Motion Blur (strength={strength})", step="fx")
        duration = self._get_duration(input_path)
        self.run_ffmpeg_with_progress(cmd, duration, progress_callback)

    def apply_vignette(self, input_path: str, output_path: str, angle: float = 0.5):
        return self.apply_vignette_with_progress(input_path, output_path, lambda p: None, angle)

    def apply_vignette_with_progress(self, input_path: str, output_path: str, progress_callback, angle: float = 0.5):
        vf = f"vignette=angle=PI*{angle}"
        encoder_args = self.get_video_encoder_args()
        cmd = [self.ffmpeg_path, "-y", "-i", input_path, "-vf", vf, *encoder_args, "-c:a", "copy", output_path]
        self.log_ffmpeg_command(cmd, f"Vignette (angle={angle})", step="fx")
        duration = self._get_duration(input_path)
        self.run_ffmpeg_with_progress(cmd, duration, progress_callback)

    def apply_speed_ramp(self, input_path: str, output_path: str, slow_start: float = 0, slow_end: float = 0, speed_factor: float = 0.5):
        return self.apply_speed_ramp_with_progress(input_path, output_path, lambda p: None, slow_start, slow_end, speed_factor)

    def apply_speed_ramp_with_progress(self, input_path: str, output_path: str, progress_callback, slow_start: float = 0, slow_end: float = 0, speed_factor: float = 0.5):
        # Complex filter for speed ramping (slow down specific section)
        # setpts for video, atempo for audio
        inv_speed = 1.0 / speed_factor
        vf = f"setpts=if(between(T\\,{slow_start}\\,{slow_end}),PTS*{inv_speed},PTS)"
        af = f"atempo={speed_factor}" if speed_factor >= 0.5 else "atempo=0.5,atempo=0.5"
        encoder_args = self.get_video_encoder_args()
        cmd = [self.ffmpeg_path, "-y", "-i", input_path, "-vf", vf, "-af", af, *encoder_args, output_path]
        self.log_ffmpeg_command(cmd, f"Speed Ramp ({slow_start}-{slow_end}s @ {speed_factor}x)", step="fx")
        duration = self._get_duration(input_path)
        self.run_ffmpeg_with_progress(cmd, duration, progress_callback)

    def apply_chroma_key(self, input_path: str, output_path: str, background_path: str, similarity: float = 0.3, blend: float = 0.1):
        return self.apply_chroma_key_with_progress(input_path, output_path, lambda p: None, background_path, similarity, blend)

    def apply_chroma_key_with_progress(self, input_path: str, output_path: str, progress_callback, background_path: str = None, similarity: float = 0.3, blend: float = 0.1):
        if not background_path or not os.path.exists(background_path):
            import shutil
            shutil.copy(input_path, output_path)
            return
        
        # Overlay input on background using chromakey filter
        filter_complex = f"[0:v]chromakey=0x00FF00:{similarity}:{blend}[ckout];[1:v][ckout]overlay[outv]"
        encoder_args = self.get_video_encoder_args()
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", background_path,
            "-i", input_path,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "1:a?",
            *encoder_args,
            output_path
        ]
        self.log_ffmpeg_command(cmd, "Chroma Key", step="fx")
        duration = self._get_duration(input_path)
        self.run_ffmpeg_with_progress(cmd, duration, progress_callback)

    def duck_audio(self, input_path: str, output_path: str, music_path: str = None, duck_level_db: float = -15, release_ms: int = 500):
        return self.duck_audio_with_progress(input_path, output_path, lambda p: None, music_path, duck_level_db, release_ms)

    def duck_audio_with_progress(self, input_path: str, output_path: str, progress_callback, music_path: str = None, duck_level_db: float = -15, release_ms: int = 500):
        if not music_path or not os.path.exists(music_path):
            import shutil
            shutil.copy(input_path, output_path)
            return

        # Sidechain compression audio ducking
        filter_complex = f"[1:a]volume=0.3[music];[0:a][music]sidechaincompress=threshold=0.1:ratio=6:release={release_ms}[ducked];[0:v]copy[outv]"
        encoder_args = self.get_video_encoder_args()
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-i", music_path,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "[ducked]",
            *encoder_args,
            output_path
        ]
        self.log_ffmpeg_command(cmd, "Audio Ducking", step="fx")
        duration = self._get_duration(input_path)
        self.run_ffmpeg_with_progress(cmd, duration, progress_callback)

    def apply_ken_burns(self, input_path: str, output_path: str, zoom_start: float = 1.0, zoom_end: float = 1.3, direction: str = "in"):
        return self.apply_ken_burns_with_progress(input_path, output_path, lambda p: None, zoom_start, zoom_end, direction)

    def apply_ken_burns_with_progress(self, input_path: str, output_path: str, progress_callback, zoom_start: float = 1.0, zoom_end: float = 1.3, direction: str = "in"):
        # Zoompan filter for Ken Burns effect
        duration = self._get_duration(input_path)
        fps = 30
        total_frames = int(duration * fps)
        
        if direction == "in":
            zoom_expr = f"zoom='min(zoom+0.0015,{zoom_end})':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        else:
            zoom_expr = f"zoom='max(zoom-0.0015,{zoom_start})':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            
        vf = f"zoompan={zoom_expr}:d={total_frames}:s=1080x1920:fps={fps}"
        encoder_args = self.get_video_encoder_args()
        cmd = [self.ffmpeg_path, "-y", "-i", input_path, "-vf", vf, *encoder_args, "-c:a", "copy", output_path]
        self.log_ffmpeg_command(cmd, f"Ken Burns ({direction})", step="fx")
        self.run_ffmpeg_with_progress(cmd, duration, progress_callback)
