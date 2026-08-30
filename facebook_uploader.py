"""
Facebook Reels Uploader — upload clips ke Facebook Page via Meta Graph API.

Mengikuti pola manifest-based seperti youtube_uploader.py dan tiktok_uploader.py.
API Flow per clip:
  1. Create Reel session      (POST /{PAGE_ID}/video_reels, upload_phase=start)
  2. Upload binary file       (POST ke upload_url)
  3. Finish: publish/schedule (POST /{PAGE_ID}/video_reels, upload_phase=finish)
  4. Poll status              (GET /{VIDEO_ID}?fields=status)

Adapted dari opensource-clipping ``facebook_uploader/uploader.py``.
"""

import json
import os
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from utils.logger import debug_log, log_error

# ==============================================================================
# META PLATFORM LIMITS
# ==============================================================================
META_REEL_RATE_LIMIT_24H = 30
META_SCHEDULE_MIN_MINUTES = 10
META_SCHEDULE_MAX_DAYS = 29

DEFAULT_FB_CONFIG = {
    "enabled": False,
    "page_id": "",
    "access_token": "",
    "graph_version": "v25.0",
    "tz_name": "Asia/Makassar",
    "interval_hours": 5,
    "test_mode": False,
}


def load_fb_config() -> dict:
    """Baca konfigurasi Facebook dari config.json (key 'facebook_uploader')
    dengan fallback ke environment variables. Return dict yang sudah digabung."""
    merged = dict(DEFAULT_FB_CONFIG)

    try:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                root = json.load(f)
            fb = root.get("facebook_uploader", {}) or {}
            if isinstance(fb, dict):
                merged.update({k: v for k, v in fb.items() if v not in (None, "")})
    except Exception as e:
        log_error(f"Gagal memuat config.json untuk facebook: {e}", e)

    if not merged["page_id"]:
        merged["page_id"] = os.environ.get("META_PAGE_ID", "").strip()
    if not merged["access_token"]:
        merged["access_token"] = os.environ.get("META_PAGE_ACCESS_TOKEN", "").strip()
    if not merged["graph_version"].strip():
        merged["graph_version"] = os.environ.get("META_GRAPH_VERSION", "v25.0").strip()

    return merged


def get_meta_config(fb_config: dict = None) -> dict:
    """Validasi config dan kembalikan struktur Graph API yang siap pakai."""
    fb_config = fb_config or load_fb_config()
    page_id = str(fb_config.get("page_id", "")).strip()
    token = str(fb_config.get("access_token", "")).strip()
    version = str(fb_config.get("graph_version", "v25.0")).strip() or "v25.0"

    if not page_id:
        raise RuntimeError(
            "Facebook belum dikonfigurasi. Set 'facebook_uploader.page_id' "
            "di config.json (atau env META_PAGE_ID)."
        )
    if not token:
        raise RuntimeError(
            "Facebook belum dikonfigurasi. Set 'facebook_uploader.access_token' "
            "di config.json (atau env META_PAGE_ACCESS_TOKEN)."
        )

    return {
        "page_id": page_id,
        "access_token": token,
        "graph_version": version,
        "base_url": f"https://graph.facebook.com/{version}",
        "tz_name": str(fb_config.get("tz_name", "Asia/Makassar")),
        "interval_hours": float(fb_config.get("interval_hours", 5)),
        "test_mode": bool(fb_config.get("test_mode", False)),
    }


def _auth_headers(config: dict) -> dict:
    return {"Authorization": f"Bearer {config['access_token']}"}


# ==============================================================================
# AUTH & SCHEDULE
# ==============================================================================

def validate_page_token(config: dict) -> dict:
    """GET /me?fields=id,name — validasi Page Access Token."""
    resp = requests.get(
        f"{config['base_url']}/me",
        headers=_auth_headers(config),
        params={"fields": "id,name"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Token validation failed: {data['error']}")
    return data


def get_latest_future_schedule(config: dict) -> datetime | None:
    """Cari scheduled_publish_time terakhir yang masih di masa depan."""
    tz = ZoneInfo(config.get("tz_name", "Asia/Makassar"))
    now = datetime.now(tz)
    latest_dt = None
    url = f"{config['base_url']}/{config['page_id']}/scheduled_posts"
    params = {"fields": "id,scheduled_publish_time", "limit": "100"}

    debug_log("🔎 Mengecek scheduled posts di Facebook Page...")
    while url:
        try:
            resp = requests.get(url, headers=_auth_headers(config), params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            debug_log(f"  ⚠️ Gagal membaca scheduled posts: {e}")
            break
        if "error" in data:
            debug_log(f"  ⚠️ Error: {data['error'].get('message', data['error'])}")
            break
        for post in data.get("data", []):
            ts = post.get("scheduled_publish_time")
            if ts is None:
                continue
            try:
                dt = datetime.fromtimestamp(int(ts), tz=tz)
            except (ValueError, TypeError):
                continue
            if dt <= now:
                continue
            if latest_dt is None or dt > latest_dt:
                latest_dt = dt
        paging = data.get("paging", {})
        next_url = paging.get("next")
        url = next_url if next_url else None
        params = {}

    if latest_dt:
        debug_log(f"  ✅ Scheduled terakhir: {latest_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    else:
        debug_log("  ℹ️ Belum ada post terjadwal di masa depan.")
    return latest_dt


# ==============================================================================
# UPLOAD FLOW (4-step Meta API)
# ==============================================================================

def create_reel_session(config: dict) -> dict:
    """POST /{PAGE_ID}/video_reels (upload_phase=start)."""
    resp = requests.post(
        f"{config['base_url']}/{config['page_id']}/video_reels",
        headers=_auth_headers(config),
        data={"upload_phase": "start"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Create reel session failed: {data['error']}")
    video_id = data.get("video_id")
    upload_url = data.get("upload_url")
    if not video_id or not upload_url:
        raise RuntimeError(f"Unexpected response dari create session: {data}")
    return {"video_id": video_id, "upload_url": upload_url}


def upload_reel_binary(upload_url: str, file_path: str, token: str) -> bool:
    """POST binary ke upload_url (header OAuth — bukan Bearer)."""
    file_size = os.path.getsize(file_path)
    headers = {
        "Authorization": f"OAuth {token}",
        "offset": "0",
        "file_size": str(file_size),
        "Content-Type": "application/octet-stream",
    }
    with open(file_path, "rb") as f:
        resp = requests.post(upload_url, headers=headers, data=f, timeout=600)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Upload binary failed: {data}")
    return True


def poll_reel_status(
    config: dict,
    video_id: str,
    timeout_seconds: int = 300,
    poll_interval: int = 10,
) -> dict:
    """Poll status Reel sampai publishing_phase.status == complete."""
    url = f"{config['base_url']}/{video_id}"
    params = {"fields": "status"}
    start_time = time.time()
    last_data = {}
    processing_complete = False

    while True:
        elapsed = time.time() - start_time
        try:
            resp = requests.get(url, headers=_auth_headers(config), params=params, timeout=30)
            if not resp.ok:
                try:
                    error_body = resp.json()
                except ValueError:
                    error_body = resp.text
                raise RuntimeError(f"Status request gagal HTTP {resp.status_code}: {error_body}")
            data = resp.json()
            last_data = data
        except Exception as exc:
            debug_log(f"   ⚠️ Gagal mengecek status video: {exc}")
            if elapsed >= timeout_seconds:
                return {"complete": False, "publish_status": None,
                        "processing_complete": processing_complete,
                        "status": {}, "raw": last_data}
            time.sleep(poll_interval)
            continue

        status = data.get("status", {})
        video_status = status.get("video_status", "")
        processing_phase = status.get("processing_phase", {})
        publishing_phase = status.get("publishing_phase", {})
        processing_status = processing_phase.get("status", "")
        publishing_status = publishing_phase.get("status", "")
        processing_complete = (
            processing_status in {"complete", "completed"} or video_status == "ready"
        )
        publishing_complete = publishing_status in {"complete", "completed"}

        debug_log(
            f"   ... video={video_status}, processing={processing_status}, "
            f"publishing={publishing_status}, "
            f"publish_status={publishing_phase.get('publish_status', '-')}"
        )

        for phase_name, phase_data in (("processing_phase", processing_phase),
                                       ("publishing_phase", publishing_phase)):
            if phase_data.get("status") == "error":
                raise RuntimeError(f"Facebook Reel gagal pada {phase_name}: {phase_data}")
        if video_status == "error":
            raise RuntimeError(f"Facebook Reel video_status=error: {status}")

        if publishing_complete:
            return {
                "complete": True,
                "publish_status": publishing_phase.get("publish_status", "unknown"),
                "processing_complete": True,
                "publishing_complete": True,
                "status": status,
                "raw": data,
            }
        if elapsed >= timeout_seconds:
            debug_log(f"   ℹ️ Publishing belum selesai setelah {timeout_seconds} detik.")
            return {
                "complete": False,
                "publish_status": publishing_phase.get("publish_status"),
                "processing_complete": processing_complete,
                "publishing_complete": False,
                "status": status,
                "raw": data,
            }
        time.sleep(poll_interval)


def finish_reel(
    config: dict,
    video_id: str,
    description: str,
    title: str,
    video_state: str = "PUBLISHED",
    scheduled_timestamp: int | None = None,
) -> dict:
    """POST /{PAGE_ID}/video_reels (upload_phase=finish); publish atau schedule."""
    url = f"{config['base_url']}/{config['page_id']}/video_reels"
    payload = {
        "video_id": video_id,
        "upload_phase": "finish",
        "video_state": video_state,
        "description": description,
        "title": title,
    }
    if video_state == "SCHEDULED":
        if scheduled_timestamp is None:
            raise ValueError("scheduled_timestamp wajib diisi untuk video_state=SCHEDULED")
        payload["scheduled_publish_time"] = str(scheduled_timestamp)

    resp = requests.post(url, headers=_auth_headers(config), data=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Finish reel failed: {data['error']}")
    if not data.get("success"):
        raise RuntimeError(f"Unexpected finish response: {data}")
    return data


# ==============================================================================
# MANIFEST HELPERS
# ==============================================================================

def load_json_file(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log_error(f"Gagal membaca JSON {path}: {e}", e)
        return default if default is not None else []


def save_json_file(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_nonempty_file(path):
    return bool(path) and os.path.exists(path) and os.path.isfile(path) and os.path.getsize(path) > 0


def normalize_text(text):
    return " ".join(str(text or "").split()).strip()


def count_recent_uploads(manifest_rows: list, hours: int = 24) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    count = 0
    for row in manifest_rows:
        uploaded_at_str = row.get("fb_uploaded_at_utc")
        if not uploaded_at_str:
            continue
        try:
            uploaded_at = datetime.fromisoformat(uploaded_at_str.replace("Z", "+00:00"))
            if uploaded_at >= cutoff:
                count += 1
        except (ValueError, TypeError):
            continue
    return count


def get_upload_candidates(render_manifest):
    return [item for item in (render_manifest or [])
            if item.get("status") == "success"
            and is_nonempty_file(item.get("video_path"))]


def get_manifest_row_by_rank(manifest_rows, rank):
    for row in manifest_rows or []:
        if row.get("rank") == rank:
            return row
    return None


def refresh_existing_facebook_statuses(config: dict, manifest_rows: list,
                                       updated_manifest_file: str) -> None:
    """Sinkronkan status Reel yang pending/scheduled dengan Meta."""
    modified = False
    target_statuses = {"pending", "uploaded", "scheduled_processing", "scheduled"}
    for row in manifest_rows:
        video_id = row.get("fb_video_id")
        status = row.get("fb_upload_status")
        if not video_id or status not in target_statuses:
            continue
        debug_log(f"🔄 Sinkronkan status Facebook Video {video_id} (status: {status})...")
        try:
            resp = requests.get(f"{config['base_url']}/{video_id}",
                                headers=_auth_headers(config),
                                params={"fields": "status"}, timeout=15)
            if not resp.ok:
                debug_log(f"   ⚠️ Request status gagal HTTP {resp.status_code}")
                continue
            data = resp.json()
            fb_status = data.get("status", {})
            publishing_phase = fb_status.get("publishing_phase", {})
            processing_status = fb_status.get("processing_phase", {}).get("status", "")
            video_status = fb_status.get("video_status", "")
            publish_status = publishing_phase.get("publish_status", "")
            new_status = None
            if publishing_phase.get("status") in {"complete", "completed"}:
                new_status = "published" if publish_status == "published" else "scheduled"
            elif processing_status in {"complete", "completed"} or video_status == "ready":
                if status in {"uploaded", "scheduled_processing"}:
                    new_status = "pending"
            row["fb_status_checked_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if new_status and new_status != status:
                row["fb_upload_status"] = new_status
                row["fb_publish_status_raw"] = fb_status
                debug_log(f"   ✅ Status diperbarui: {status} ➔ {new_status}")
                modified = True
            else:
                modified = True
        except Exception as e:
            debug_log(f"   ⚠️ Gagal sinkron status {video_id}: {e}")
    if modified and os.path.dirname(updated_manifest_file):
        save_json_file(updated_manifest_file, manifest_rows)
        debug_log("💾 Manifest terupdate setelah sinkronisasi status.")


def _get_clip_metadata(item: dict) -> tuple:
    title = (
        item.get("youtube_title_final")
        or item.get("title_inggris")
        or item.get("title_indonesia")
        or item.get("title")
        or f"Clip Rank {item.get('rank', '?')}"
    )
    title = normalize_text(title)[:100]
    description = (
        item.get("youtube_description_final")
        or item.get("tiktok_caption_final")
        or item.get("description")
        or ""
    )
    description = normalize_text(description)
    return title, description


# ==============================================================================
# MAIN PIPELINE
# ==============================================================================

def upload_manifest_to_facebook(
    manifest_file: str = "outputs/render_manifest.json",
    result_file: str = "outputs/fb_upload_results.json",
    updated_manifest_file: str = "outputs/render_manifest_fb_uploaded.json",
    fb_config: dict = None,
) -> list:
    """
    Pipeline utama: baca manifest → tentukan schedule → upload Reels satu per satu.

    Scheduling:
      - Baca jadwal terakhir dari Meta, jaga cursor last_assigned_time.
      - Clip pertama publish sekarang bila antrian kosong, selain itu dijadwalkan.
      - Clip berikutnya dijadwalkan pada last_assigned_time + interval.
      - Jika SCHEDULED gagal: batch BERHENTI (tanpa fallback ke PUBLISHED).
    """
    config = get_meta_config(fb_config)

    page_info = validate_page_token(config)
    if str(page_info.get("id")) != str(config["page_id"]):
        raise RuntimeError(
            "META_PAGE_ACCESS_TOKEN tidak cocok dengan META_PAGE_ID. "
            f"Token mengarah ke ID {page_info.get('id')}, "
            f"konfigurasi memakai {config['page_id']}."
        )
    debug_log(f"✅ Token valid untuk Page: {page_info.get('name')} (ID: {page_info.get('id')})")

    source_manifest_file = manifest_file
    if updated_manifest_file and os.path.exists(updated_manifest_file) and os.path.getsize(updated_manifest_file) > 0:
        source_manifest_file = updated_manifest_file
        debug_log(f"📂 Menggunakan manifest Facebook sebelumnya: {source_manifest_file}")

    render_manifest = load_json_file(source_manifest_file, default=[])
    if not render_manifest:
        debug_log(f"⚠️ {source_manifest_file} kosong / tidak ditemukan.")
        return []

    refresh_existing_facebook_statuses(config, render_manifest, updated_manifest_file)

    candidates = get_upload_candidates(render_manifest)
    if not candidates:
        debug_log("⚠️ Tidak ada item yang siap diupload.")
        return []

    pending_items = []
    for item in candidates:
        if item.get("fb_video_id"):
            debug_log(f"⏭️ Skip Rank {item.get('rank')} — sudah punya Facebook Video ID "
                      f"(status={item.get('fb_upload_status') or 'unknown'})")
            continue
        pending_items.append(item)

    recent_count = count_recent_uploads(render_manifest, hours=24)
    remaining_quota = max(0, META_REEL_RATE_LIMIT_24H - recent_count)
    debug_log(f"📊 Rate limit: {recent_count}/{META_REEL_RATE_LIMIT_24H} Reels dalam 24 jam terakhir.")
    if remaining_quota == 0:
        debug_log("🛑 Rate limit tercapai! Upload dibatalkan.")
        return []
    if len(pending_items) > remaining_quota:
        debug_log(f"⚠️ Hanya {remaining_quota} dari {len(pending_items)} clip yang diupload (sisa kuota 24 jam).")
        pending_items = pending_items[:remaining_quota]

    if config.get("test_mode") and pending_items:
        pending_items = pending_items[:1]
        debug_log("🧪 Mode test aktif: hanya upload 1 item pertama.")
    if not pending_items:
        debug_log("⚠️ Semua item success sudah pernah diupload ke Facebook.")
        return []

    tz = ZoneInfo(config["tz_name"])
    now = datetime.now(tz)
    interval = timedelta(hours=config["interval_hours"])
    last_assigned_time = get_latest_future_schedule(config)

    upload_results = []
    updated_manifest = deepcopy(render_manifest)

    debug_log(f"🚀 Upload {len(pending_items)} clip ke Facebook Page... "
              f"(interval {config['interval_hours']} jam, tz {config['tz_name']})")

    for idx, item in enumerate(pending_items):
        rank = item.get("rank")
        manifest_row = get_manifest_row_by_rank(updated_manifest, rank)
        title, description = _get_clip_metadata(item)
        video_path = item.get("video_path", "")

        if idx > 0:
            latest_meta_schedule = get_latest_future_schedule(config)
            if latest_meta_schedule is not None:
                last_assigned_time = max(last_assigned_time or latest_meta_schedule, latest_meta_schedule)

        if last_assigned_time is None:
            video_state = "PUBLISHED"
            scheduled_at = None
            scheduled_timestamp = None
            last_assigned_time = datetime.now(tz)
            mode_label = "PUBLISH NOW"
        else:
            scheduled_at = last_assigned_time + interval
            now_check = datetime.now(tz)
            min_schedule = now_check + timedelta(minutes=META_SCHEDULE_MIN_MINUTES)
            max_schedule = now_check + timedelta(days=META_SCHEDULE_MAX_DAYS)
            if scheduled_at < min_schedule:
                debug_log(f"   ⚠️ Jadwal {scheduled_at.strftime('%H:%M:%S')} terlalu dekat; "
                          f"dibump ke {min_schedule.strftime('%H:%M:%S')}.")
                scheduled_at = min_schedule
            if scheduled_at > max_schedule:
                debug_log(f"   🛑 Jadwal {scheduled_at.strftime('%Y-%m-%d %H:%M')} melebihi 29 hari. "
                          f"Batch dihentikan.")
                break
            scheduled_timestamp = int(scheduled_at.timestamp())
            video_state = "SCHEDULED"
            last_assigned_time = scheduled_at
            mode_label = f"SCHEDULED → {scheduled_at.strftime('%Y-%m-%d %H:%M %Z')}"

        debug_log(f"\n{'=' * 60}\n=== Clip {idx + 1}/{len(pending_items)} — Rank {rank} ===\n"
                  f"Judul  : {title}\nMode   : {mode_label}\n{'=' * 60}")

        video_id = None
        post_id = None
        try:
            session = create_reel_session(config)
            video_id = session["video_id"]
            upload_url = session["upload_url"]
            debug_log(f"   ✅ Session created. Video ID: {video_id}")
            debug_log(f"   ⬆️ Uploading: {os.path.basename(video_path)} "
                      f"({os.path.getsize(video_path) / 1024 / 1024:.1f} MB)...")
            upload_reel_binary(upload_url, video_path, config["access_token"])
            debug_log("   ✅ Upload binary berhasil.")
            finish_result = finish_reel(
                config=config, video_id=video_id, description=description,
                title=title, video_state=video_state, scheduled_timestamp=scheduled_timestamp,
            )
            post_id = finish_result.get("post_id")
            debug_log(f"   ✅ Reel {video_state} berhasil didaftarkan!" +
                      (f"📌 Post ID: {post_id}" if post_id else ""))
            status_result = poll_reel_status(config=config, video_id=video_id)

            if status_result["complete"]:
                publish_status = status_result.get("publish_status", "unknown")
                final_status = ({True: "published", False: "scheduled"}.get(
                    publish_status == "published", publish_status))
                if publish_status == "published":
                    final_status = "published"
                elif publish_status == "scheduled":
                    final_status = "scheduled"
                else:
                    final_status = publish_status
            else:
                final_status = "pending" if status_result.get("processing_complete") else "uploaded"

            if manifest_row is not None:
                manifest_row["fb_upload_status"] = final_status
                manifest_row["fb_publish_status_raw"] = status_result.get("status", {})
                manifest_row["fb_video_id"] = video_id
                manifest_row["fb_post_id"] = post_id
                manifest_row["fb_video_state"] = video_state
                manifest_row["fb_scheduled_at"] = (
                    scheduled_at.strftime("%Y-%m-%d %H:%M:%S %Z") if scheduled_at else None
                )
                manifest_row["fb_uploaded_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                manifest_row["fb_upload_error"] = None

            upload_results.append({
                "rank": rank, "status": final_status,
                "filename": os.path.basename(video_path),
                "video_id": video_id, "post_id": post_id, "mode": video_state,
                "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
            })

            if final_status == "published":
                debug_log(f"   ✅ Reel published. Video ID: {video_id}")
            elif final_status == "scheduled":
                debug_log(f"   ✅ Reel dijadwalkan ke {scheduled_at.strftime('%Y-%m-%d %H:%M:%S %Z') if scheduled_at else '-'}. Video ID: {video_id}")
            elif final_status == "pending":
                debug_log(f"   ⏳ Reel diterima Meta, menunggu publishing. Video ID: {video_id}")
            else:
                debug_log(f"   ⚠️ Reel diterima Meta dengan status '{final_status}'. Video ID: {video_id}")

        except Exception as e:
            err = str(e)
            if manifest_row is not None:
                manifest_row["fb_upload_status"] = "failed"
                manifest_row["fb_upload_error"] = err
                if video_id:
                    manifest_row["fb_video_id"] = video_id
            upload_results.append({
                "rank": rank, "status": "failed",
                "filename": os.path.basename(video_path), "mode": video_state, "error": err,
            })
            log_error(f"Upload gagal Rank {rank}: {err}", e)
            debug_log("   🛑 Batch dihentikan karena upload gagal.")
            break

        save_json_file(result_file, upload_results)
        save_json_file(updated_manifest_file, updated_manifest)

    save_json_file(result_file, upload_results)
    save_json_file(updated_manifest_file, updated_manifest)
    debug_log(f"💾 Hasil disimpan ke {result_file}")
    debug_log(f"💾 Manifest terupdate disimpan ke {updated_manifest_file}")
    return upload_results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Upload manifest ke Facebook Reels.")
    parser.add_argument("--manifest", default="outputs/render_manifest.json",
                        help="Path manifest (default: outputs/render_manifest.json)")
    parser.add_argument("--result", default="outputs/fb_upload_results.json",
                        help="Path hasil upload (default: outputs/fb_upload_results.json)")
    parser.add_argument("--updated-manifest", default="outputs/render_manifest_fb_uploaded.json",
                        help="Path manifest terupdate")
    args = parser.parse_args()

    upload_manifest_to_facebook(
        manifest_file=args.manifest,
        result_file=args.result,
        updated_manifest_file=args.updated_manifest,
    )