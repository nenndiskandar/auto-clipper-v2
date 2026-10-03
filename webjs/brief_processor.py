#!/usr/bin/env python3
# Brief Processor - extract brief.pdf (PyMuPDF) fallback deskripsi,
# lalu LLM extract JSON terstruktur.
# Usage: brief_processor.py <session_dir>
# stdout JSON {ok:true, ai_brief:{...}} + tulis session_dir/ai_brief.json
import sys, os, json, re, traceback, urllib.request
from pathlib import Path

APP_DIR = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from config.config_manager import ConfigManager
from utils.logger import debug_log

def extract_pdf_text(pdf_path: str) -> str:
    try:
        import fitz
    except ImportError:
        try:
            import pymupdf as fitz
        except ImportError:
            debug_log("[brief] PyMuPDF tidak ada, skip pdf extract")
            return ""
    try:
        doc = fitz.open(pdf_path)
        texts = []
        for page in doc:
            try:
                t = page.get_text("text")
                if t:
                    texts.append(t)
            except Exception:
                continue
        doc.close()
        return "\n".join(texts).strip()
    except Exception as e:
        debug_log(f"[brief] pdf extract gagal {e}")
        return ""

def download_brief_pdf(url: str, dest: Path):
    if not url:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as res:
            data = res.read()
            if len(data) < 500:
                debug_log(f"[brief] pdf download too small {len(data)}")
                return None
            dest.write_bytes(data)
            debug_log(f"[brief] pdf downloaded {len(data)} bytes -> {dest}")
            return dest
    except Exception as e:
        debug_log(f"[brief] pdf download gagal {url[:80]} {e}")
        return None

def call_llm_extract(brief_text: str, cfg):
    prov = (cfg.get("ai_providers") or {}).get("highlight_finder") or {}
    api_key = prov.get("api_key") or cfg.get("api_key") or ""
    base_url = prov.get("base_url") or cfg.get("base_url") or "https://api.openai.com/v1"
    model = prov.get("model") or cfg.get("model") or "opencodes"
    if not api_key:
        debug_log("[brief] no api_key, skip llm")
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)
        sys_prompt = (
            "Kamu adalah asisten yang mengekstrak brief campaign menjadi JSON terstruktur.\n"
            "Tugas: baca brief campaign berikut dan ekstrak info penting untuk bikin konten clip.\n\n"
            "OUTPUT HARUS JSON VALID TANPA TEKS LAIN, format:\n"
            "{\n"
            '  "duration_min": 15,\n'
            '  "duration_max": 30,\n'
            '  "duration_target": 25,\n'
            '  "hashtags_required": ["#tagWajib1", "#tagWajib2"],\n'
            '  "hashtags_suggested": ["#relevan1", "#relevan2", "#relevan3"],\n'
            '  "hook_wajib": "kalimat hook yang wajib ada di clip / null jika tidak ada",\n'
            '  "larangan": ["hal yang dilarang"],\n'
            '  "music_query": "judul lagu / sound tiktok yang diminta / null",\n'
            '  "music_url": "link youtube/tiktok mp3 jika disebut eksplisit / null",\n'
            '  "subtitle_style": "pop atau karaoke atau null",\n'
            '  "aspect_ratio": "9:16 atau null",\n'
            '  "summary": "ringkasan 1 kalimat isi brief"\n'
            "}\n\n"
            "ATURAN:\n"
            "- duration ambil dari brief (misal '15-30 detik', '60-90 detik'). Jika tidak disebut, default 15-30.\n"
            "- hashtags_required = hashtag yang WAJIB ada di brief (tanpa duplikat, pakai #). hashtags_suggested = tambah 2-3 hashtag relevan yang tidak ada di brief tapi masih nyambung niche (jangan halu brand lain).\n"
            "- hook_wajib = kalimat/keyword yang brief suruh wajib tampilkan (misal 'pintunya selalu terbuka'). Jika tidak ada, null.\n"
            "- music_query = jika brief suruh pakai backsound tertentu (misal 'pakai sound tiktok X' atau 'backsound ceria'), tulis query-nya. Jika tidak ada, null.\n"
            "- music_url = jika brief kasih link youtube/tiktok/soundcloud mp3 eksplisit, tulis url. Jika tidak ada, null.\n"
            "- Jangan halusinasi, hanya ambil yang ada di brief.\n"
        )
        user_content = f"BRIEF CAMPAIGN:\n{brief_text[:6000]}\n\nEkstrak jadi JSON sesuai format di atas. Hanya JSON, tanpa teks lain."
        debug_log(f"[brief] calling LLM {base_url} model={model} brief_len={len(brief_text)}")
        # retry like highlight finder (2 attempts) + larger max_tokens for reasoning model
        max_attempts = int(__import__("os").environ.get("AI_BRIEF_ATTEMPTS", "2"))
        resp = None
        raw = ""
        for attempt in range(1, max_attempts+1):
            try:
                debug_log(f"[brief] LLM attempt {attempt}/{max_attempts}")
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0.5,
                    max_tokens=2200,
                    timeout=120.0,
                )
                if resp and resp.choices and resp.choices[0].message and (resp.choices[0].message.content or "").strip():
                    raw = (resp.choices[0].message.content or "").strip()
                    debug_log(f"[brief] LLM raw attempt {attempt} {raw[:400]}")
                    break
                # handle reasoning_content field (some routers put thinking there)
                try:
                    rc = getattr(resp.choices[0].message, "reasoning_content", None) or getattr(resp.choices[0].message, "reasoning", None)
                    if rc and isinstance(rc, str) and len(rc.strip())>20:
                        # try extract JSON from reasoning if content empty
                        import re as _re
                        m2 = _re.search(r"\{[\s\S]*\}", rc)
                        if m2:
                            raw = m2.group(0)
                            debug_log(f"[brief] LLM reasoning extracted {raw[:300]}")
                            break
                except:
                    pass
                debug_log(f"[brief] LLM attempt {attempt} empty content finish={getattr(resp.choices[0],'finish_reason',None) if resp and resp.choices else None} raw={repr((resp.choices[0].message.content if resp and resp.choices and resp.choices[0].message else None))[:200]}")
                if attempt < max_attempts:
                    import time as _t; _t.sleep(1.5)
                    continue
            except Exception as e:
                debug_log(f"[brief] LLM attempt {attempt} err {e}")
                if attempt >= max_attempts:
                    raise
                import time as _t; _t.sleep(1.5)
        if not raw:
            debug_log("[brief] LLM all attempts empty")
            return None
        debug_log(f"[brief] LLM raw final {raw[:400]}")
        if raw.startswith("```"):
            raw = re.sub(r"```json?\n?", "", raw)
            raw = re.sub(r"```\n?", "", raw)
            raw = raw.strip()
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            raw = m.group(0)
        raw = re.sub(r",\s*}", "}", raw)
        raw = re.sub(r",\s*]", "]", raw)
        j = json.loads(raw)
        if "hashtags_required" not in j:
            j["hashtags_required"] = []
        if "hashtags_suggested" not in j:
            j["hashtags_suggested"] = []
        for k in ("hashtags_required", "hashtags_suggested"):
            if isinstance(j.get(k), list):
                j[k] = [("#" + x.lstrip("#")) if x and not x.startswith("#") else x for x in j[k] if isinstance(x, str) and x.strip()]
        debug_log(f"[brief] LLM extracted ok hashtags={j.get('hashtags_required')}")
        return j
    except Exception as e:
        debug_log(f"[brief] LLM extract gagal {e}")
        traceback.print_exc()
        return None

def fallback_regex(brief_text: str) -> dict:
    hashtags = re.findall(r"#\w+", brief_text)
    hashtags = list(dict.fromkeys(hashtags))[:6]
    dur_min, dur_max = 15, 30
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*(detik|second|s\b)", brief_text, re.I)
    if m:
        try:
            dur_min = int(m.group(1)); dur_max = int(m.group(2))
        except:
            pass
    else:
        m2 = re.search(r"durasi\s*(\d+)\s*(detik|second)", brief_text, re.I)
        if m2:
            dur_max = int(m2.group(1)); dur_min = max(12, dur_max - 15)
        else:
            m3 = re.search(r"minimal\s*(\d+)\s*detik", brief_text, re.I)
            if m3:
                dur_min = int(m3.group(1)); dur_max = max(dur_min + 15, 30)
    # hashtags_suggested: tambah 2-3 relevan jika LLM gagal
    # ambil keyword judul + niche film/movie secara heuristik
    suggested = []
    low = brief_text.lower()
    # niche map
    if "film" in low or "trailer" in low or "movie" in low:
        for cand in ["#filmindonesia", "#reviewfilm", "#filmviral"]:
            if cand.lower() not in [h.lower() for h in hashtags] and cand not in suggested:
                suggested.append(cand)
                if len(suggested) >= 2:
                    break
    if not suggested:
        for cand in ["#fyp", "#viral", "#kontenkreator"]:
            if cand.lower() not in [h.lower() for h in hashtags]:
                suggested.append(cand)
                if len(suggested) >= 2:
                    break
    # music_query heuristik
    music_query = None
    music_url = None
    # cari link youtube/tiktok di brief
    urls = re.findall(r"https?://[^\s]+", brief_text)
    for u in urls:
        if "youtube.com" in u or "youtu.be" in u or "tiktok.com" in u:
            # mungkin music_url jika konteks backsound/music/sound
            ctx = brief_text[max(0, brief_text.find(u)-120): brief_text.find(u)+len(u)+40].lower()
            if any(k in ctx for k in ["music", "backsound", "sound", "lagu", "audio", "backsound"]):
                music_url = u.strip(".,)")
                break
    if not music_url:
        m_music = re.search(r"(backsound|sound\s*tiktok|lagu)\s*[:\-]?\s*([^\n]{5,60})", brief_text, re.I)
        if m_music:
            q = m_music.group(2).strip()
            if len(q) > 4 and "http" not in q.lower():
                music_query = q[:60].strip(" .,")
    # hook_wajib heuristik: cari frase wajib
    hook_wajib = None
    m_hook = re.search(r"(hook|wajib.*tampilkan|pintunya selalu terbuka|wajib.*hook)[^\n]{0,80}", brief_text, re.I)
    if m_hook:
        hook_wajib = m_hook.group(0).strip()[:120]
        if len(hook_wajib) < 8:
            hook_wajib = None
    return {
        "duration_min": dur_min,
        "duration_max": dur_max,
        "duration_target": (dur_min + dur_max)//2,
        "hashtags_required": hashtags[:4],
        "hashtags_suggested": suggested[:3],
        "hook_wajib": hook_wajib,
        "larangan": [],
        "music_query": music_query,
        "music_url": music_url,
        "subtitle_style": None,
        "aspect_ratio": "9:16",
        "summary": brief_text[:180].strip(),
    }

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "session_dir required"}, ensure_ascii=False))
        sys.exit(1)
    session_dir = Path(sys.argv[1])
    if not session_dir.exists():
        print(json.dumps({"ok": False, "error": f"session_dir not found {session_dir}"}, ensure_ascii=False))
        sys.exit(1)
    brief_file = session_dir / "campaign_brief.json"
    if not brief_file.exists():
        print(json.dumps({"ok": False, "error": "campaign_brief.json not found"}, ensure_ascii=False))
        sys.exit(1)
    try:
        brief_data = json.loads(brief_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"read brief failed {e}"}, ensure_ascii=False))
        sys.exit(1)
    file_brief_url = brief_data.get("file_brief_url") or ""
    description = brief_data.get("description") or ""
    brief_text = ""
    pdf_path = session_dir / "_temp" / "brief.pdf"
    if file_brief_url and file_brief_url.startswith("http"):
        dl = download_brief_pdf(file_brief_url, pdf_path)
        if dl and dl.exists():
            txt = extract_pdf_text(str(dl))
            if len(txt.strip()) >= 100:
                brief_text = txt.strip()
                debug_log(f"[brief] pdf text {len(brief_text)} chars")
            else:
                debug_log(f"[brief] pdf text too short {len(txt)} fallback deskripsi")
    if not brief_text or len(brief_text.strip()) < 100:
        brief_text = description.strip()
        if not brief_text:
            brief_text = brief_data.get("title","") + " " + " ".join(brief_data.get("tags",[]) or [])
        debug_log(f"[brief] using fallback deskripsi {len(brief_text)} chars")
    if not brief_text or len(brief_text.strip()) < 20:
        print(json.dumps({"ok": False, "error": "brief kosong, tidak bisa ekstrak"}, ensure_ascii=False))
        sys.exit(1)
    app_dir = Path(APP_DIR)
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    ai_json = call_llm_extract(brief_text, cfg)
    if not ai_json:
        debug_log("[brief] fallback regex")
        ai_json = fallback_regex(brief_text)
        ai_json["_fallback"] = "regex"
    else:
        ai_json["_fallback"] = False
    ai_json["brief_text"] = brief_text[:6000]
    ai_json["brief_source"] = "pdf" if (pdf_path.exists() and len(brief_text) > 100 and file_brief_url) else "description"
    ai_json["campaign_id"] = brief_data.get("campaign_id") or brief_data.get("public_id")
    ai_json["campaign_title"] = brief_data.get("title","")
    out_path = session_dir / "ai_brief.json"
    try:
        out_path.write_text(json.dumps(ai_json, indent=2, ensure_ascii=False), encoding="utf-8")
        debug_log(f"[brief] wrote {out_path}")
    except Exception as e:
        debug_log(f"[brief] write failed {e}")
    print(json.dumps({"ok": True, "ai_brief": ai_json, "brief_len": len(brief_text)}, ensure_ascii=False))

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        try:
            print(json.dumps({"ok": False, "error": traceback.format_exc()[-1500:]}, ensure_ascii=False))
        except:
            pass
        sys.exit(1)
