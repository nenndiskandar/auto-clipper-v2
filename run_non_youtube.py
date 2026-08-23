async def run_non_youtube(chat_id: int, url: str, context: ContextTypes.DEFAULT_TYPE, status_msg):
    """Non-YouTube path: download video, split into clips, render without AI/subtitle."""
    loop = asyncio.get_running_loop()
    status_holder = {"last_log_send": 0, "last_progress_pct": -1, "last_progress_send": 0}
    log_cb, prog_cb = make_callbacks(chat_id, context.bot, loop, status_holder)

    with session_lock:
        busy = ACTIVE_CHATS.get(chat_id)
    if busy:
        await status_msg.edit_text(
            f"⏳ Masih ada proses yang berjalan: {busy}.\n"
            "Tunggu selesai atau batalkan dengan /cancel_session.",
        )
        return

    with session_lock:
        ACTIVE_CHATS[chat_id] = "Non-YouTube - Download & Process"

    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    core = get_core_instance(cfg_mgr, log_cb=log_cb, progress_cb=prog_cb)
    cfg = cfg_mgr.config

    session_id = url.split("/")[-1].split("?")[0] or datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = cfg_mgr.output_dir / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    session_data = {
        "session_dir": str(session_dir),
        "url": url,
        "video_info": {"title": session_id, "channel": "Unknown"},
        "highlights": [],
        "created_at": datetime.now().isoformat(),
        "status": "downloading",
    }
    session_data_file = session_dir / "session_data.json"
    with open(session_data_file, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2, ensure_ascii=False)

    try:
        # Step 1: Download full video
        await status_msg.edit_text("⏳ *Non-YouTube:* Downloading video...")
        core.temp_dir = session_dir / "_temp"
        core.temp_dir.mkdir(parents=True, exist_ok=True)
        core.last_session_dir = str(session_dir)

        video_path, srt_path, video_info = await loop.run_in_executor(
            None, core.download_video, url
        )
        if not video_path or not Path(video_path).exists():
            await status_msg.edit_text("❌ Download video gagal.")
            return

        session_data["video_info"] = video_info or {}
        session_data["status"] = "processing"
        with open(session_data_file, "w", encoding="utf-8") as f:
            json.dump(session_data, f, indent=2, ensure_ascii=False)

        # Step 2: Get video duration and split into 60-90s clips
        await status_msg.edit_text("⏳ *Non-YouTube:* Analisis durasi & membagi clip...")
        probe_cmd = [
            core.ffmpeg_path or "ffmpeg", "-i", video_path,
            "-show_entries", "format=duration",
            "-v", "quiet", "-of", "csv=p=0"
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=1, timeout=30)
        total_duration = float(result.stdout.strip() or "0")

        if total_duration <= 0:
            await status_msg.edit_text("❌ Gagal mendeteksi durasi video.")
            return

        clip_duration = 80  # target detik per clip
        clips_count = max(1, int(total_duration // clip_duration))

        highlights = []
        for i in range(clips_count):
            start_sec = i * clip_duration
            end_sec = min((i + 1) * clip_duration, total_duration)
            highlights.append({
                "title": f"Clip {i + 1}/{clips_count}",
                "start_time": f"{int(start_sec // 3600):02d}:{int((start_sec % 3600) // 60):02d}:{start_sec % 60:06.3f}",
                "end_time": f"{int(end_sec // 3600):02d}:{int((end_sec % 3600) // 60):02d}:{end_sec % 60:06.3f}",
                "start_second": start_sec,
                "end_second": end_sec,
                "duration_seconds": round(end_sec - start_sec, 1),
                "virality_score": 8,
            })
        session_data["highlights"] = highlights

        # Step 3: Build selection UI with all highlights pre-selected
        all_indices = list(range(len(highlights)))
        selected = set(all_indices)
        USER_SESSIONS[chat_id] = {
            "core": core,
            "url": url,
            "session_data": session_data,
            "highlights": highlights,
            "loop": loop,
            "selected": selected,
            "cancel_event": threading.Event(),
        }
        core.is_cancelled = USER_SESSIONS[chat_id]["cancel_event"].is_set
        USER_SESSIONS[chat_id]["cancel_event"].clear()

        # Step 4: Show highlight menu (all pre-selected)
        keyboard = []
        for idx, h in enumerate(highlights):
            mark = "☑️"
            keyboard.append([InlineKeyboardButton(
                f"{mark} [{h['start_time']} → {h['end_time']}] {h['title']}",
                callback_data=f"sel_{idx}"
            )])
        keyboard.append([
            InlineKeyboardButton("✅ Semua", callback_data="sel_all"),
            InlineKeyboardButton("🎬 Proses Selected", callback_data="sel_process"),
        ])
        await status_msg.edit_text(
            f"🎯 *Non-YouTube:* Video dibagi jadi {len(highlights)} clip ({int(total_duration)}s total).\n"
            "Semua clip sudah dipilih. Klik *Proses Selected* untuk mulai render:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.exception("Error in non-YouTube flow")
        short_err = str(e)[:300].replace("`", "'")
        await status_msg.edit_text(f"❌ *Error:*\n`{short_err}`", parse_mode="Markdown")
    finally:
        with session_lock:
            ACTIVE_CHATS.pop(chat_id, None)
