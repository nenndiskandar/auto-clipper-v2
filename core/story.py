"""
core/story.py — Story Clip Mode (multi-source narrative assembly).

Loads a ``sources.json`` (source registry) and a ``story_recipe.json``
(hook + highlight scenes per clip), downloads/caches all source videos,
optionally transcribes them, then assembles clean hook & highlight videos
via FFmpeg (trim → normalize → concat with cut/crossfade).

Adapted from opensource-clipping ``clipping/story_runner.py`` + ``story/*``.
"""

import json
import os
import shutil
import subprocess

from utils.logger import debug_log

SUPPORTED_PLATFORMS = {"youtube", "tiktok", "instagram", "gdrive", "local"}

RATIO_MAP = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "3:4": (1080, 1440),
    "4:5": (1080, 1350),
}


# ==============================================================================
# LOADERS
# ==============================================================================

def load_sources(path: str) -> dict:
    """Parse & validate a ``sources.json`` file → {sid: entry}."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Sources file tidak ditemukan: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    entries = raw.get("sources", [])
    if not entries:
        raise ValueError(f"sources.json kosong / tak punya 'sources': {path}")

    registry = {}
    for idx, src in enumerate(entries):
        missing = {"id", "name", "platform"} - set(src.keys())
        if missing:
            raise ValueError(f"Source #{idx}: field wajib hilang {missing}")
        sid = src["id"]
        platform = src["platform"]
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"Source '{sid}': platform '{platform}' tidak dikenal.")
        if platform == "local":
            if not src.get("local_path") or not os.path.exists(src.get("local_path", "")):
                raise ValueError(f"Source '{sid}': local_path tidak valid.")
        elif not src.get("url"):
            raise ValueError(f"Source '{sid}': platform '{platform}' butuh 'url'.")
        if sid in registry:
            raise ValueError(f"Duplicate source id: '{sid}'")
        registry[sid] = src
    debug_log(f"[Story] Loaded {len(registry)} source(s) dari {os.path.basename(path)}")
    return registry


def load_recipe(path: str, source_registry: dict) -> dict:
    """Parse & validate ``story_recipe.json`` (returns recipe dict)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Recipe file tidak ditemukan: {path}")
    with open(path, "r", encoding="utf-8") as f:
        recipe = json.load(f)
    clips = recipe.get("clips", [])
    if not clips:
        raise ValueError(f"story_recipe.json tak punya 'clips': {path}")

    seen = set()
    for clip in clips:
        missing = {"clip_id", "title", "hook", "highlight"} - set(clip.keys())
        if missing:
            raise ValueError(f"Clip {clip.get('clip_id', '?')}: field wajib hilang {missing}")
        cid = clip["clip_id"]
        if cid in seen:
            raise ValueError(f"Duplicate clip_id: {cid}")
        seen.add(cid)
        for section in ("hook", "highlight"):
            scenes = clip[section].get("scenes", [])
            if not scenes:
                raise ValueError(f"Clip #{cid}: {section}.scenes kosong.")
            for i, scene in enumerate(scenes):
                if scene.get("source_id") not in source_registry:
                    raise ValueError(
                        f"Clip #{cid} → {section} → scene #{i}: source_id "
                        f"'{scene.get('source_id')}' tidak ada.")
    debug_log(f"[Story] Loaded {len(clips)} clip(s) dari {os.path.basename(path)}")
    return recipe


# ==============================================================================
# DOWNLOAD & CACHE
# ==============================================================================

def get_cache_dir(outputs_dir: str) -> str:
    cache_dir = os.path.join(outputs_dir, "story_cache")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _download_single_source(source: dict, cache_dir: str, download_height: str = "max") -> str:
    sid = source["id"]
    platform = source["platform"]
    cached_path = os.path.join(cache_dir, f"{sid}.mp4")

    if os.path.exists(cached_path) and os.path.getsize(cached_path) > 10_000:
        debug_log(f"[Story] '{sid}' sudah ada di cache, skip.")
        return cached_path

    if platform == "local":
        debug_log(f"[Story] Menyalin file lokal: {source['local_path']}")
        shutil.copy2(source["local_path"], cached_path)
        return cached_path

    url = source["url"]
    debug_log(f"[Story] Mendownload {platform}: {url}")
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp diperlukan untuk story sources non-local.")

    def _progress_hook(d):
        if d.get("status") == "downloading":
            pct = d.get("_percent_str", "").strip()
            debug_log(f"[Story] {sid} download: {pct}")

    if platform == "gdrive":
        # Google Drive via gdown (fallback: yt-dlp generic)
        try:
            import gdown
            gdown.download(url, cached_path, quiet=False)
        except ImportError:
            debug_log("[Story] gdown tidak terinstal, coba yt-dlp generic...")
        if not (os.path.exists(cached_path) and os.path.getsize(cached_path) > 10_000):
            raise RuntimeError(f"gdrive download gagal untuk '{sid}'")
        return cached_path

    format_sel = f"bestvideo[height<={download_height}]+bestaudio/best[height<={download_height}]/best"
    ydl_opts = {
        "format": format_sel,
        "merge_output_format": "mp4",
        "outtmpl": cached_path,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_progress_hook],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        # Some TikTok/Instagram URLs only work via generic extractor with cookies never needed.
        ydl_opts["format"] = "best"
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e2:
            raise RuntimeError(f"Download gagal '{sid}': {e2}") from e

    if not (os.path.exists(cached_path) and os.path.getsize(cached_path) > 10_000):
        # yt-dlp may output different ext; search cache dir for the sid prefix
        import glob
        hits = glob.glob(os.path.join(cache_dir, sid + ".*"))
        for h in hits:
            if h != cached_path and os.path.getsize(h) > 10_000:
                os.replace(h, cached_path)
                break
    if not (os.path.exists(cached_path) and os.path.getsize(cached_path) > 10_000):
        raise RuntimeError(f"Download gagal untuk source '{sid}'.")
    return cached_path


def download_all_sources(source_registry: dict, cache_dir: str, download_height: str = "max") -> dict:
    paths = {}
    for sid, source in source_registry.items():
        try:
            paths[sid] = _download_single_source(source, cache_dir, download_height)
        except Exception as e:
            debug_log(f"[Story] GAGAL download '{sid}': {e}")
    return paths


# ==============================================================================
# WHISPER TRANSCRIPTION (for story sources)
# ==============================================================================

def transcribe_sources(cached_paths: dict, cache_dir: str, whisper_model: str = "medium") -> dict:
    """
    Transcribe cached source videos using Faster-Whisper (if installed).
    Returns {sid: {"transkrip": str, "segmen": list, "path": str}}.
    """
    results = {}
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        debug_log("[Story] faster-whisper tidak tersedia, skip transkripsi.")
        return {}

    model = None
    for sid, video_path in cached_paths.items():
        transcript_path = os.path.join(cache_dir, f"{sid}_transcript.json")
        if os.path.exists(transcript_path):
            try:
                with open(transcript_path, "r", encoding="utf-8") as f:
                    results[sid] = json.load(f)
                continue
            except Exception:
                pass
        if not os.path.exists(video_path):
            continue
        debug_log(f"[Story] Transcribing '{sid}'...")
        if model is None:
            model = WhisperModel(whisper_model, device="cpu", compute_type="int8")
        try:
            segmen = []
            transkrip = []
            segs, info = model.transcribe(video_path)
            for seg in segs:
                words = [{"word": w.word, "start": w.start, "end": w.end} for w in (seg.words or [])]
                segmen.append({
                    "start": seg.start, "end": seg.end,
                    "text": seg.text.strip(), "words": words,
                })
                transkrip.append(seg.text.strip())
            result = {
                "source_id": sid,
                "transkrip": " ".join(transkrip),
                "segmen": segmen,
                "path": transcript_path,
            }
            with open(transcript_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            results[sid] = result
        except Exception as e:
            debug_log(f"[Story] '{sid}' gagal ditranskrip: {e}")
    return results


# ==============================================================================
# FFMPEG ASSEMBLY
# ==============================================================================

def _run_ffmpeg(cmd: list, label: str = "", ffmpeg_path: str = "ffmpeg") -> None:
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=600,
        )
    except subprocess.CalledProcessError as e:
        stderr_text = (e.stderr or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"FFmpeg gagal{' (' + label + ')' if label else ''}: {stderr_text[:500]}") from e


def trim_scene(video_path, start, end, output_path, reencode=True, ffmpeg_path="ffmpeg"):
    duration = end - start
    if reencode:
        cmd = [
            ffmpeg_path, "-y", "-ss", f"{start:.3f}", "-i", video_path,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-avoid_negative_ts", "make_zero", output_path,
        ]
    else:
        cmd = [
            ffmpeg_path, "-y", "-ss", f"{start:.3f}", "-i", video_path,
            "-t", f"{duration:.3f}", "-c", "copy",
            "-avoid_negative_ts", "make_zero", output_path,
        ]
    _run_ffmpeg(cmd, label=f"trim {start:.1f}-{end:.1f}", ffmpeg_path=ffmpeg_path)
    return output_path


def _normalize_scene_segment(input_path, output_path, target_width, target_height, target_fps=30, ffmpeg_path="ffmpeg"):
    vf = (
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
        f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={target_fps},format=yuv420p"
    )
    cmd = [
        ffmpeg_path, "-y", "-i", input_path, "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-shortest", output_path,
    ]
    _run_ffmpeg(cmd, label="normalize", ffmpeg_path=ffmpeg_path)
    return output_path


def concat_scenes(scene_paths, output_path, transition="cut", ffmpeg_path="ffmpeg"):
    if not scene_paths:
        raise ValueError("Tidak ada scene untuk di-concat.")
    if len(scene_paths) == 1:
        shutil.copy2(scene_paths[0], output_path)
        return output_path
    if transition == "crossfade":
        return _concat_with_crossfade(scene_paths, output_path, ffmpeg_path)
    return _concat_hard_cut(scene_paths, output_path, ffmpeg_path)


def _concat_hard_cut(scene_paths, output_path, ffmpeg_path="ffmpeg"):
    list_path = output_path + ".concat_list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in scene_paths:
            f.write(f"file '{os.path.abspath(p).replace(chr(92), '/')}'\n")
    cmd = [ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", output_path]
    _run_ffmpeg(cmd, label="concat_hard_cut", ffmpeg_path=ffmpeg_path)
    if os.path.exists(list_path):
        os.remove(list_path)
    return output_path


def _concat_with_crossfade(scene_paths, output_path, ffmpeg_path="ffmpeg", fade_duration=0.5):
    if len(scene_paths) == 2:
        cmd = [
            ffmpeg_path, "-y", "-i", scene_paths[0], "-i", scene_paths[1],
            "-filter_complex",
            f"[0:v][1:v]xfade=transition=fade:duration={fade_duration}:offset=0[v];"
            f"[0:a][1:a]acrossfade=d={fade_duration}[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "aac",
            output_path,
        ]
        _run_ffmpeg(cmd, label="crossfade", ffmpeg_path=ffmpeg_path)
        return output_path

    temp_dir = output_path + "_xfade_tmp"
    os.makedirs(temp_dir, exist_ok=True)
    current = scene_paths[0]
    try:
        for i in range(1, len(scene_paths)):
            temp_out = os.path.join(temp_dir, f"xfade_{i}.mp4")
            cmd = [
                ffmpeg_path, "-y", "-i", current, "-i", scene_paths[i],
                "-filter_complex",
                f"[0:v][1:v]xfade=transition=fade:duration={fade_duration}:offset=0[v];"
                f"[0:a][1:a]acrossfade=d={fade_duration}[a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "aac",
                temp_out,
            ]
            _run_ffmpeg(cmd, label=f"crossfade_{i}", ffmpeg_path=ffmpeg_path)
            current = temp_out
        shutil.copy2(current, output_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return output_path


# ==============================================================================
# ASSEMBLY (hook / highlight)
# ==============================================================================

def _resolve_scene_path(scene, source_registry, cache_dir):
    sid = scene["source_id"]
    src = source_registry[sid]
    if src["platform"] == "local":
        return src["local_path"]
    return os.path.join(cache_dir, f"{sid}.mp4")


def _assemble_scenes(scene_list, clip_config, source_registry, cache_dir, temp_dir,
                     ratio, prefix, ffmpeg_path="ffmpeg", transition="cut"):
    target_w, target_h = RATIO_MAP.get(ratio, (1080, 1920))
    parts = []
    for idx, scene in enumerate(scene_list):
        start = scene.get("start")
        end = scene.get("end")
        if start is None or end is None:
            continue
        video_path = _resolve_scene_path(scene, source_registry, cache_dir)
        if not os.path.exists(video_path):
            continue
        trimmed = os.path.join(temp_dir, f"{prefix}_scene_{idx}_trim.mp4")
        trim_scene(video_path, start, end, trimmed, reencode=True, ffmpeg_path=ffmpeg_path)
        normed = os.path.join(temp_dir, f"{prefix}_scene_{idx}_norm.mp4")
        _normalize_scene_segment(trimmed, normed, target_w, target_h, ffmpeg_path=ffmpeg_path)
        parts.append(normed)
    if not parts:
        return None
    return concat_scenes(parts, os.path.join(temp_dir, f"{prefix}.mp4"),
                         transition=transition, ffmpeg_path=ffmpeg_path)


def assemble_hook(clip_config, source_registry, cache_dir, output_dir, ratio="9:16", ffmpeg_path="ffmpeg"):
    cid = clip_config["clip_id"]
    scenes = clip_config["hook"].get("scenes", [])
    temp_dir = os.path.join(output_dir, f"_temp_hook_{cid}")
    os.makedirs(temp_dir, exist_ok=True)
    try:
        out = _assemble_scenes(scenes, clip_config, source_registry, cache_dir,
                               temp_dir, ratio, f"hook_{cid}", ffmpeg_path, transition="cut")
        if not out:
            debug_log(f"[Story] Hook #{cid}: tidak ada scene valid.")
            return None
        final = os.path.join(output_dir, f"hook_{cid}.mp4")
        shutil.move(out, final)
        debug_log(f"[Story] hook_{cid}.mp4 siap.")
        return final
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def assemble_highlight(clip_config, source_registry, cache_dir, output_dir, ratio="9:16", ffmpeg_path="ffmpeg"):
    cid = clip_config["clip_id"]
    highlight = clip_config["highlight"]
    scenes = highlight.get("scenes", [])
    raw_transition = highlight.get("transition", "cut")
    transition_map = {"smooth": "crossfade", "crossfade": "crossfade"}
    transition = transition_map.get(raw_transition, "cut")
    temp_dir = os.path.join(output_dir, f"_temp_highlight_{cid}")
    os.makedirs(temp_dir, exist_ok=True)
    try:
        out = _assemble_scenes(scenes, clip_config, source_registry, cache_dir,
                               temp_dir, ratio, f"highlight_{cid}", ffmpeg_path, transition)
        if not out:
            debug_log(f"[Story] Highlight #{cid}: tidak ada scene valid.")
            return None
        final = os.path.join(output_dir, f"highlight_{cid}.mp4")
        shutil.move(out, final)
        debug_log(f"[Story] highlight_{cid}.mp4 siap ({len(scenes)} scene, {raw_transition}).")
        return final
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ==============================================================================
# MAIN PIPELINE
# ==============================================================================

def run_story_pipeline(
    sources_json_path: str,
    story_recipe_path: str,
    outputs_dir: str,
    whisper_model: str = "medium",
    skip_download: bool = False,
    download_source_height: str = "max",
    ratio: str = "9:16",
    ffmpeg_path: str = "ffmpeg",
) -> list[dict]:
    """
    Run the full Story Clip pipeline.

    Args:
        sources_json_path: Path to ``sources.json``.
        story_recipe_path: Path to ``story_recipe.json``.
        outputs_dir: Output base directory.
        whisper_model: Local Faster-Whisper model size for transcription.
        skip_download: Skip download (use existing cache / local paths).
        download_source_height: Desired download resolution.
        ratio: Target aspect ratio for assembled clips.
        ffmpeg_path: FFmpeg executable.

    Returns:
        Manifest (list of dicts) with hook/highlight output paths.
    """
    debug_log("=" * 60)
    debug_log("[Story] Story Clip — Multi-Source Narrative Assembly")
    debug_log("=" * 60)

    source_registry = load_sources(sources_json_path)
    cache_dir = get_cache_dir(outputs_dir)

    if skip_download:
        cached_paths = {}
        for sid, src in source_registry.items():
            if src["platform"] == "local":
                cached_paths[sid] = src["local_path"]
            else:
                cached = os.path.join(cache_dir, f"{sid}.mp4")
                if os.path.exists(cached):
                    cached_paths[sid] = cached
    else:
        cached_paths = download_all_sources(source_registry, cache_dir, download_source_height)

    transcripts = transcribe_sources(cached_paths, cache_dir, whisper_model)
    debug_log(f"[Story] {len(transcripts)}/{len(cached_paths)} source ditranskrip.")

    recipe = load_recipe(story_recipe_path, source_registry)
    defaults = recipe.get("default_settings", {})
    if ratio is None:
        ratio = defaults.get("ratio", "9:16")

    story_output_dir = os.path.join(outputs_dir, "story_clips")
    os.makedirs(story_output_dir, exist_ok=True)

    manifest = []
    for clip_config in sorted(recipe.get("clips", []), key=lambda c: c["clip_id"]):
        cid = clip_config["clip_id"]
        clip_dir = os.path.join(story_output_dir, f"clip_{cid}")
        os.makedirs(clip_dir, exist_ok=True)
        hook_path = assemble_hook(clip_config, source_registry, cache_dir, clip_dir,
                                  ratio, ffmpeg_path)
        highlight_path = assemble_highlight(clip_config, source_registry, cache_dir, clip_dir,
                                            ratio, ffmpeg_path)
        manifest.append({
            "clip_id": cid,
            "title": clip_config.get("title", f"Clip {cid}"),
            "hook_path": hook_path,
            "highlight_path": highlight_path,
            "status": "ok" if (hook_path and highlight_path) else "partial",
            "metadata": clip_config.get("metadata", {}),
        })

    manifest_path = os.path.join(outputs_dir, "story_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    debug_log(f"[Story] Selesai! Manifest: {manifest_path}")
    return manifest