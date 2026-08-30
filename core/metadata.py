"""
core/metadata.py — Metadata Normalization, Enrichment & Account Classification.

Normalizes highlight metadata produced by the LLM (many providers return
slightly different field names) and enriches it with cross-platform fields:
    - youtube_title_final / youtube_description_final / youtube_tags_final
    - tiktok_caption_final / tiktok_title_id_final / tiktok_caption_id_final
    - klasifikasi_akun (Business / Life / Creator / Muslim)
    - viral_score 1-100 rating with per-item breakdown

Adapted from opensource-clipping ``clipping/metadata.py``.
"""

import json


# ==============================================================================
# HELPERS
# ==============================================================================

def _normalize_spaces(text):
    return " ".join(str(text or "").split()).strip()


def _trim_title(text, max_len=100):
    text = _normalize_spaces(text)
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0].strip()
    return cut if cut else text[:max_len].strip()


def _normalize_hashtags(text, min_tags=2, max_tags=3):
    parts = _normalize_spaces(text).split()
    clean = []
    seen = set()
    for p in parts:
        if not p:
            continue
        if not p.startswith("#"):
            p = "#" + p.lstrip("#")
        key = p.lower()
        if key not in seen:
            seen.add(key)
            clean.append(p)
        if len(clean) >= max_tags:
            break
    return " ".join(clean), len(clean)


def _normalize_keyword_tags(tags, max_items=8):
    if not isinstance(tags, list):
        tags = []
    out = []
    seen = set()
    for t in tags:
        x = _normalize_spaces(t)
        if not x:
            continue
        key = x.lower()
        if key not in seen:
            seen.add(key)
            out.append(x)
        if len(out) >= max_items:
            break
    return out


def _build_youtube_description(hook, context, hashtags, source_url=None):
    parts = [
        _normalize_spaces(hook),
        _normalize_spaces(context),
        _normalize_spaces(hashtags),
    ]
    desc = "\n\n".join([p for p in parts if p]).strip()
    if source_url:
        desc += f"\n\nSource: {source_url}"
    return desc


def _build_tiktok_caption(caption, hashtags):
    caption = _normalize_spaces(caption)
    hashtags = _normalize_spaces(hashtags)
    if caption and hashtags:
        return f"{caption}\n{hashtags}"
    return caption or hashtags


def _looks_indonesian(text):
    text = f" {_normalize_spaces(text).lower()} "
    indikator = [
        " yang ", " dan ", " untuk ", " dengan ", " karena ", " adalah ",
        " bisa ", " tidak ", " lebih ", " dalam ", " pada ", " agar ",
        " dari ", " ini ", " itu ", " juga ", " kalau ", " saat ",
        " tentang ", " bikin ", " banget ", " jadi ", " sudah ",
    ]
    return any(w in text for w in indikator)


# ==============================================================================
# ACCOUNT CLASSIFICATION (heuristik berbasis keyword)
# ==============================================================================

CATEGORY_KEYWORDS = {
    "Business": [
        "bisnis", "bisnik", "bisnis", "bisiness", "business", "uang", "duit",
        "sales", "marketing", "investasi", "trading", "keuangan", "finansial",
        "startup", "wirausaha", "produk", "brand", "ekonomi",
    ],
    "Life": [
        "keluarga", "keluarga", "sehari", "sehari-hari", "life", "kehidupan",
        "rumah", "rutinitas", "vlog", "the journey", "daily",
    ],
    "Creator": [
        "creator", "kreator", "konten", "content", "editing", "videographer",
        "youtuber", "photography", "studio", "editing video", "tips edit",
    ],
    "Muslim": [
        "islam", "muslim", "dakwah", "quran", "alquran", "hadits", "sholat",
        "sunnah", "ustadz", "akhlak", "iman", "doa",
    ],
}


def klasifikasikan_akun(hasil: list[dict]) -> dict:
    """
    Classify the target account type from the aggregated highlight content.

    Args:
        hasil: List of highlight dicts (used for keyword voting), or a single
               dict (di-coerce ke list).

    Returns:
        dict with ``akun_tujuan``, ``tipe_akun``, ``alasan``.
    """
    if isinstance(hasil, dict):
        hasil = [hasil]
    text_combined = " "
    for item in hasil or []:
        for key in ("title", "title_indonesia", "title_inggris", "description_hook",
                    "description_context", "tiktok_caption_id", "tiktok_caption"):
            text_combined += f" {_normalize_spaces(item.get(key, ''))} "
    text_low = text_combined.lower()

    scores = {}
    for category, keywords in CATEGORY_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in text_low)
        if count:
            scores[category] = count

    # Default by simple keyword count; tie → Creator (platform-agnostic).
    if not scores:
        return {
            "akun_tujuan": "TikTok",
            "tipe_akun": "Creator",
            "alasan": "Tidak ada keyword yang cukup — default Creator/TikTok.",
        }

    best = max(scores, key=scores.get)
    platform = {"Business": "TikTok, Reels, YouTube", "Life": "TikTok, Reels",
                "Creator": "TikTok, YouTube, Reels", "Muslim": "TikTok, Reels, YouTube"}
    return {
        "akun_tujuan": platform.get(best, "TikTok"),
        "tipe_akun": best,
        "alasan": f"Topik mendominasi bertema {best} ({scores[best]} sinyal keyword).",
    }


def _hitung_viral_score(item: dict) -> float:
    """
    Compute a viral score 1-100 from highlight content signals.
    """
    score = 50.0
    # Explicit AI-provided virality (0-10 or 0-100), boosted
    raw = item.get("virality_score") or item.get("viral_score") or 0
    try:
        raw = float(raw)
        if raw <= 10:
            score = raw * 10.0
        else:
            score = raw
    except (TypeError, ValueError):
        pass

    hook = _normalize_spaces(item.get("hook_text") or item.get("hook", ""))
    if len(hook) >= 8:
        score = min(100, score + 8)
    if len(hook) >= 25:
        score = min(100, score + 6)

    desc_len = len(_normalize_spaces(item.get("description_hook") or ""))
    if desc_len >= 40:
        score = min(100, score + 5)
    elif desc_len < 10:
        score = max(1, score - 8)

    dur = 0.0
    try:
        dur = float(item.get("end_time", 0)) - float(item.get("start_time", 0))
    except (TypeError, ValueError):
        pass
    if 15 <= dur <= 60:
        score = min(100, score + 10)   # sweet spot for short-form
    elif dur > 120:
        score = max(1, score - 12)

    # Engagement keywords boost
    emo_kw = ["cara", "rahasia", "mistake", "kesalahan", "tutorial", "belajar",
              "valuable", "luar biasa", "suscess", "tips"]
    if any(kw in (hook + str(item.get("title", ""))).lower() for kw in emo_kw):
        score = min(100, score + 6)

    return round(max(1.0, min(100.0, score)), 1)


# ==============================================================================
# MAIN API
# ==============================================================================

def normalize_and_validate(hasil_json: list[dict]) -> list[dict]:
    """
    Normalize and enrich highlight metadata, adding ``*_final`` fields,
    ``klasifikasi_akun``, and recomputed ``viral_score``.

    Returns the sorted list (by viral_score, descending).
    """
    valid_items = []
    for item in hasil_json:
        if not isinstance(item, dict):
            continue

        # 1. Flatten: model sometimes nests everything under "metadata".
        if isinstance(item.get("metadata"), dict):
            for k, v in item["metadata"].items():
                if k not in item:
                    item[k] = v

        # 2. Aliasing for non-Gemini providers.
        rank = item.get("rank") or item.get("peringkat") or item.get("no") or "?"
        item["rank"] = rank

        it_st = item.get("start_time") or item.get("timing_klip_start") or item.get("clip_start") or item.get("start")
        it_en = item.get("end_time") or item.get("timing_klip_end") or item.get("clip_end") or item.get("end")
        item["start_time"] = float(it_st) if it_st is not None else 0.0
        item["end_time"] = float(it_en) if it_en is not None else 0.0

        # Hook alias → dict
        if isinstance(item.get("hook"), str) and "hook_start_time" in item:
            item["hook"] = {
                "text": item["hook"],
                "start_time": item.get("hook_start_time", item["start_time"]),
                "end_time": item.get("hook_end_time", item["end_time"]),
            }
        if not isinstance(item.get("hook"), dict):
            ct = item["start_time"]
            item["hook"] = {"text": str(item.get("hook", "")), "start_time": ct, "end_time": ct + 3.0}
        item["hook_text"] = str(item["hook"].get("text") or item.get("hook_text") or "")

        # Normalize text fields
        item["title_indonesia"] = _trim_title(item.get("title_indonesia", item.get("title", "")))
        item["title_inggris"] = _trim_title(item.get("title_inggris", item.get("title", "")))
        item["description_hook"] = _normalize_spaces(item.get("description_hook", ""))
        item["description_context"] = _normalize_spaces(item.get("description_context", ""))
        item["hastag"] = _normalize_spaces(item.get("hastag") or item.get("hashtag") or "")
        item["tiktok_title_id"] = _normalize_spaces(item.get("tiktok_title_id", ""))
        item["tiktok_caption_id"] = _normalize_spaces(item.get("tiktok_caption_id", ""))
        item["tiktok_caption"] = _normalize_spaces(item.get("tiktok_caption", ""))
        item["keyword_tags"] = _normalize_keyword_tags(item.get("keyword_tags", []))

        hastag_clean, _ = _normalize_hashtags(item["hastag"])
        item["hastag"] = hastag_clean

        # Enriched fields
        item["youtube_title_final"] = item["title_inggris"]
        item["youtube_description_final"] = _build_youtube_description(
            item.get("description_hook", ""),
            item.get("description_context", ""),
            item.get("hastag", ""),
            source_url=item.get("source_url"),
        )
        item["youtube_tags_final"] = item.get("keyword_tags", [])
        item["tiktok_caption_final"] = _build_tiktok_caption(
            item.get("tiktok_caption", ""), item.get("hastag", ""))
        item["tiktok_title_id_final"] = item.get("tiktok_title_id", "") or item.get("title_indonesia", "")
        item["tiktok_caption_id_final"] = _build_tiktok_caption(
            item.get("tiktok_caption_id", ""), item.get("hastag", ""))

        item["viral_score"] = _hitung_viral_score(item)
        valid_items.append(item)

    # Account classification across the whole result set.
    klasifikasi = klasifikasikan_akun(valid_items)
    for item in valid_items:
        item["klasifikasi_akun"] = klasifikasi

    valid_items.sort(key=lambda x: x.get("viral_score", 0), reverse=True)
    for idx, item in enumerate(valid_items):
        item["rank"] = idx + 1
    return valid_items


def save_metadata_preview(hasil_json: list[dict], path: str = "metadata_preview.json") -> str:
    """Save normalized metadata to a JSON file and return the path."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hasil_json, f, ensure_ascii=False, indent=2)
    return path