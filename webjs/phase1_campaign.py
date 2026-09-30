#!/usr/bin/env python3
"""
Setup Master Sesi untuk Campaign TernakKlip
Menyimpan brief, deskripsi, dan metadata multi-source ke folder sesi.
"""

import sys
import os
import json
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from utils.logger import debug_log

def setup_campaign_session(campaign_id):
    debug_log(f"[*] Inisialisasi Master Sesi TernakKlip: {campaign_id}")
    
    # 1. Fetch detail campaign dari API
    url = f"https://api.ternakklip.com/api/v1/public/campaigns/{campaign_id}"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.loads(res.read().decode('utf-8'))
            if data.get('status') != 'success' or not data.get('data'):
                print(json.dumps({'ok': False, 'error': 'Campaign tidak ditemukan atau API error'}))
                return
            camp = data['data']
    except Exception as e:
        print(json.dumps({'ok': False, 'error': f"Fetch error: {str(e)}"}))
        return

    # 2. Setup direktori sesi tk_<id>
    session_id = f"tk_{campaign_id}"
    session_dir = os.path.join(ROOT, 'output', 'sessions', session_id)
    raw_dir = os.path.join(session_dir, '_temp_gdrive')
    os.makedirs(session_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)

    # 3. Format payload brief
    brief_data = {
        "campaign_id": camp.get("public_id"),
        "title": camp.get("title", ""),
        "client_name": camp.get("client_name", ""),
        "client_avatar_url": camp.get("client_avatar_url"),
        "description": camp.get("description", ""),
        "file_brief_url": camp.get("file_brief_url"),
        "share_url": camp.get("share_url"),
        "total_prize": camp.get("total_prize", 0),
        "current_prize": camp.get("current_prize", 0),
        "platform_rewards": camp.get("platform_rewards", []),
        "min_threshold": camp.get("min_threshold"),
        "max_threshold": camp.get("max_threshold"),
        "tags": [t.get("label") for t in camp.get("tags", []) if isinstance(t, dict) and "label" in t],
        "source_links": camp.get("source_links", [])
    }

    # Simpan campaign_brief.json
    brief_file = os.path.join(session_dir, 'campaign_brief.json')
    with open(brief_file, 'w', encoding='utf-8') as f:
        json.dump(brief_data, f, indent=2, ensure_ascii=False)

    # Setup session_data.json standar jika belum ada
    session_data_file = os.path.join(session_dir, 'session_data.json')
    if not os.path.exists(session_data_file):
        base_session_data = {
            "session_id": session_id,
            "status": "idle",
            "url": camp.get("source_links", [{}])[0].get("url", "") if camp.get("source_links") else "",
            "video_info": {
                "title": f"🎯 {camp.get('title', '')}",
                "channel": camp.get("client_name", "")
            },
            "campaign": brief_data,
            "highlights": []
        }
        with open(session_data_file, 'w', encoding='utf-8') as f:
            json.dump(base_session_data, f, indent=2, ensure_ascii=False)

    debug_log(f"[+] Master Sesi {session_id} siap. Menampung {len(brief_data['source_links'])} sumber bahan.")
    print(json.dumps({
        'ok': True,
        'session_id': session_id,
        'session_dir': session_dir,
        'campaign': brief_data
    }))

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(json.dumps({'ok': False, 'error': 'Campaign ID required'}))
        sys.exit(1)
    setup_campaign_session(sys.argv[1])
