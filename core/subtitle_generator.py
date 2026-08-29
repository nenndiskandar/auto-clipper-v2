import os
import re

EMOJI_MAP = {
    "uang": "💸", "duit": "💵", "kaya": "🤑", "miskin": "📉", "bisnis": "💼", "investasi": "📈", "saham": "📊",
    "sukses": "🏆", "menang": "🥇", "gagal": "❌", "kerja": "🛠️", "kantor": "🏢", "dunia": "🌍", "negara": "🇮🇩",
    "rahasia": "🤫", "bohong": "🤥", "jujur": "😇", "cinta": "❤️", "marah": "😡", "sedih": "😢", "kaget": "😱",
    "takut": "😨", "senang": "😊", "tertawa": "😂", "lucu": "🤣", "gila": "🤪", "pintar": "🧠", "bodoh": "🤡",
    "waktu": "⏱️", "jam": "⏰", "hari": "📅", "malam": "🌙", "pagi": "☀️", "cepat": "⚡", "lambat": "🐢",
    "makanan": "🍔", "kopi": "☕", "minum": "🥤", "makan": "🍽️", "tidur": "😴", "rumah": "🏠", "mobil": "🚗",
    "motor": "🏍️", "sepeda": "🚲", "pesawat": "✈️", "jalan": "🛣️", "api": "🔥", "air": "💧", "tanah": "🌱",
    "buku": "📚", "belajar": "📖", "sekolah": "🏫", "kuliah": "🎓", "guru": "👨‍🏫", "dosen": "👩‍🏫", "murid": "👨‍🎓",
    "game": "🎮", "main": "🕹️", "musik": "🎵", "lagu": "🎶", "film": "🎬", "nonton": "📺", "foto": "📷",
    "ponsel": "📱", "hp": "📱", "laptop": "💻", "komputer": "🖥️", "internet": "🌐", "website": "💻",
    "roket": "🚀", "bintang": "⭐", "bulan": "🌙", "matahari": "☀️", "awan": "☁️", "hujan": "🌧️",
    "money": "💸", "cash": "💵", "rich": "🤑", "poor": "📉", "business": "💼", "invest": "📈", "stock": "📊",
    "success": "🏆", "win": "🥇", "fail": "❌", "work": "🛠️", "office": "🏢", "world": "🌍",
    "secret": "🤫", "lie": "🤥", "honest": "😇", "love": "❤️", "angry": "😡", "sad": "😢", "shocked": "😱",
    "fear": "😨", "happy": "😊", "laugh": "😂", "funny": "🤣", "crazy": "🤪", "smart": "🧠", "dumb": "🤡",
    "time": "⏱️", "clock": "⏰", "day": "📅", "night": "🌙", "morning": "☀️", "fast": "⚡", "slow": "🐢",
    "food": "🍔", "coffee": "☕", "drink": "🥤", "eat": "🍽️", "sleep": "😴", "home": "🏠", "car": "🚗",
    "rocket": "🚀", "star": "⭐", "sun": "☀️", "cloud": "☁️", "rain": "🌧️", "book": "📚", "music": "🎵",
    "phone": "📱", "laptop": "💻", "computer": "🖥️", "internet": "🌐",
}

class SubtitleGeneratorMixin:
    def _attach_emoji(self, word_str: str) -> str:
        clean = re.sub(r'[^\w]', '', word_str).lower()
        emoji = EMOJI_MAP.get(clean, '')
        return f"{word_str} {emoji}" if emoji else word_str
    def format_time(self, seconds: float) -> str:
        """Convert seconds to ASS time format"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        centisecs = int((seconds % 1) * 100)
        return f"{hours}:{minutes:02d}:{secs:02d}.{centisecs:02d}"

    def create_ass_subtitle_karaoke(self, transcript, output_path: str, time_offset: float = 0):
        """Create ASS subtitle file with KTV-style karaoke: the WHOLE sentence is
        shown while each word lights up in yellow (PrimaryColour) as it is spoken,
        with unspoken words staying gray (SecondaryColour).
        """
        ass_content = """[Script Info]
Title: Karaoke captions
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,62,&H0000FFFF&,&H00808080&,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,400,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        words = list(getattr(transcript, 'words', None) or [])
        segments = list(getattr(transcript, 'segments', None) or [])
        
        def make_karaoke_line(chunk):
            parts = []
            for w in chunk:
                dur_cs = max(1, int(round((w.end - w.start) * 100)))
                parts.append("{\\kf%d}%s" % (dur_cs, self._attach_emoji(str(w.word).strip().upper())))
            return {
                'start': self.format_time(chunk[0].start + time_offset),
                'end': self.format_time(chunk[-1].end + time_offset),
                'text': " ".join(parts)
            }
        
        if words and segments:
            for seg in segments:
                seg_words = [w for w in words
                             if w.start >= seg.get('start', 0) - 0.15
                             and w.start <= seg.get('end', 0) + 0.15]
                if not seg_words:
                    continue
                for i in range(0, len(seg_words), 8):
                    events.append(make_karaoke_line(seg_words[i:i + 8]))
        elif words:
            current = []
            for w in words:
                if current and w.start - current[-1].end > 1.0:
                    events.append(make_karaoke_line(current))
                    current = []
                current.append(w)
            if current:
                events.append(make_karaoke_line(current))
        elif segments:
            for segment in segments:
                start = segment.get('start', 0) + time_offset
                end = segment.get('end', 0) + time_offset
                text = segment.get('text', '').strip().upper()
                if text:
                    events.append({
                        'start': self.format_time(start),
                        'end': self.format_time(end),
                        'text': text
                    })
        
        for event in events:
            ass_content += f"Dialogue: 0,{event['start']},{event['end']},Default,,0,0,0,,{event['text']}\n"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ass_content)

    def create_ass_subtitle_bounce(self, transcript, output_path: str, time_offset: float = 0):
        """Create ASS subtitle file with word-by-word bounce-in animation"""
        ass_content = """[Script Info]
Title: Bounce captions
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,62,&H0000FFFF&,&H0000FFFF&,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,400,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        words = list(getattr(transcript, 'words', None) or [])
        segments = list(getattr(transcript, 'segments', None) or [])
        
        def make_bounce_line(chunk):
            chunk_start = chunk[0].start
            parts = []
            for w in chunk:
                d = max(1, int(round((w.end - w.start) * 1000)))
                offset_ms = max(0, int(round((w.start - chunk_start) * 1000)))
                p1 = offset_ms + max(1, int(d * 0.15))
                p2 = offset_ms + max(2, int(d * 0.40))
                end_ms = offset_ms + d
                parts.append(
                    "{\\alpha&HFF&"
                    "\\t(%d,%d,1,\\alpha&H00&\\c&H00FFFF&\\fscx115\\fscy115)"
                    "\\t(%d,%d,1,\\fscx100\\fscy100)"
                    "\\t(%d,%d,1,\\c&HFFFFFF&)}%s"
                    % (offset_ms, p1, p1, p2, p2, end_ms,
                       self._attach_emoji(str(w.word).strip().upper()))
                )
            return {
                'start': self.format_time(chunk[0].start + time_offset),
                'end': self.format_time(chunk[-1].end + time_offset),
                'text': " ".join(parts)
            }
        
        if words and segments:
            for seg in segments:
                seg_words = [w for w in words
                             if w.start >= seg.get('start', 0) - 0.15
                             and w.start <= seg.get('end', 0) + 0.15]
                if not seg_words:
                    continue
                for i in range(0, len(seg_words), 8):
                    events.append(make_bounce_line(seg_words[i:i + 8]))
        elif words:
            current = []
            for w in words:
                if current and w.start - current[-1].end > 1.0:
                    events.append(make_bounce_line(current))
                    current = []
                current.append(w)
            if current:
                events.append(make_bounce_line(current))
        elif segments:
            for segment in segments:
                start = segment.get('start', 0) + time_offset
                end = segment.get('end', 0) + time_offset
                text = segment.get('text', '').strip().upper()
                if text:
                    events.append({
                        'start': self.format_time(start),
                        'end': self.format_time(end),
                        'text': text
                    })
        
        for event in events:
            ass_content += f"Dialogue: 0,{event['start']},{event['end']},Default,,0,0,0,,{event['text']}\n"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ass_content)

    def create_ass_subtitle_pop_bounce(self, transcript, output_path: str, time_offset: float = 0):
        """Create ASS subtitle file with Pop + Bounce captions"""
        ass_content = """[Script Info]
Title: Pop bounce captions
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,65,&H00FFFFFF,&H00808080&,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,400,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        words = list(getattr(transcript, 'words', None) or [])
        segments = list(getattr(transcript, 'segments', None) or [])
        CHUNK = 4
        
        def make_line(chunk):
            chunk_start = chunk[0].start
            chunk_end = chunk[-1].end
            parts = []
            for k, w in enumerate(chunk):
                d = max(1, int(round((w.end - w.start) * 1000)))
                offset_ms = max(0, int(round((w.start - chunk_start) * 1000)))
                t1 = offset_ms + max(1, int(d * 0.15))
                t2 = offset_ms + max(2, int(d * 0.30))
                t3 = offset_ms + max(3, int(d * 0.45))
                t4 = offset_ms + max(4, int(d * 0.60))
                t5 = offset_ms + d
                parts.append(
                    "{\\c&HFFFFFF&"
                    "\\t(%d,%d,1,\\c&H00FFFF&\\fscx130\\fscy130)"
                    "\\t(%d,%d,1,\\fscx90\\fscy90)"
                    "\\t(%d,%d,1,\\fscx105\\fscy105)"
                    "\\t(%d,%d,1,\\fscx100\\fscy100)"
                    "\\t(%d,%d,1,\\c&HFFFFFF&)}%s"
                    % (offset_ms, t1, t1, t2, t2, t3, t3, t4, t4, t5,
                       self._attach_emoji(str(w.word).strip().upper()))
                )
            return {
                'start': self.format_time(chunk_start + time_offset),
                'end': self.format_time(chunk_end + time_offset),
                'text': " ".join(parts)
            }
        
        if words and segments:
            for seg in segments:
                seg_words = [w for w in words
                             if w.start >= seg.get('start', 0) - 0.15
                             and w.start <= seg.get('end', 0) + 0.15]
                if not seg_words:
                    continue
                for i in range(0, len(seg_words), CHUNK):
                    events.append(make_line(seg_words[i:i + CHUNK]))
        elif words:
            current = []
            for w in words:
                if current and w.start - current[-1].end > 1.0:
                    for i in range(0, len(current), CHUNK):
                        events.append(make_line(current[i:i + CHUNK]))
                    current = []
                current.append(w)
            if current:
                for i in range(0, len(current), CHUNK):
                    events.append(make_line(current[i:i + CHUNK]))
        elif segments:
            for segment in segments:
                start = segment.get('start', 0) + time_offset
                end = segment.get('end', 0) + time_offset
                text = segment.get('text', '').strip().upper()
                if text:
                    events.append({
                        'start': self.format_time(start),
                        'end': self.format_time(end),
                        'text': text
                    })
        
        for event in events:
            ass_content += f"Dialogue: 0,{event['start']},{event['end']},Default,,0,0,0,,{event['text']}\n"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ass_content)

    def create_ass_subtitle_animated(self, transcript, output_path: str, time_offset: float = 0):
        """Create ASS subtitle file with combined Bounce + Animated word-by-word captions"""
        ass_content = """[Script Info]
Title: Bounce word-by-word captions
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,65,&H00FFFFFF,&H00808080&,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,400,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        words = list(getattr(transcript, 'words', None) or [])
        segments = list(getattr(transcript, 'segments', None) or [])
        
        def make_animated_line(chunk):
            chunk_start = chunk[0].start
            parts = []
            for k, w in enumerate(chunk):
                d = max(1, int(round((w.end - w.start) * 1000)))
                offset_ms = max(0, int(round((w.start - chunk_start) * 1000)))
                t1 = offset_ms + max(1, int(d * 0.15))
                t2 = offset_ms + max(2, int(d * 0.30))
                t3 = offset_ms + max(3, int(d * 0.45))
                t4 = offset_ms + max(4, int(d * 0.60))
                bounce = ("{\\c&H00FFFF&\\fscx0\\fscy0"
                          "\\t(%d,%d,1,\\fscx125\\fscy125)"
                          "\\t(%d,%d,1,\\fscx95\\fscy95)"
                          "\\t(%d,%d,1,\\fscx105\\fscy105)"
                          "\\t(%d,%d,1,\\fscx100\\fscy100)}%s"
                          % (offset_ms, t1, t1, t2, t2, t3, t3, t4,
                             self._attach_emoji(str(w.word).strip().upper())))
                if k == len(chunk) - 1:
                    parts.append(bounce)
                else:
                    parts.append(bounce + "{\\c&HFFFFFF&}")
            return {
                'start': self.format_time(chunk[0].start + time_offset),
                'end': self.format_time(chunk[-1].end + time_offset),
                'text': " ".join(parts)
            }
        
        if words and segments:
            for seg in segments:
                seg_words = [w for w in words
                             if w.start >= seg.get('start', 0) - 0.15
                             and w.start <= seg.get('end', 0) + 0.15]
                if not seg_words:
                    continue
                for i in range(0, len(seg_words), 8):
                    events.append(make_animated_line(seg_words[i:i + 8]))
        elif words:
            current = []
            for w in words:
                if current and w.start - current[-1].end > 1.0:
                    events.append(make_animated_line(current))
                    current = []
                current.append(w)
            if current:
                events.append(make_animated_line(current))
        elif segments:
            for segment in segments:
                start = segment.get('start', 0) + time_offset
                end = segment.get('end', 0) + time_offset
                text = segment.get('text', '').strip().upper()
                if text:
                    events.append({
                        'start': self.format_time(start),
                        'end': self.format_time(end),
                        'text': text
                    })
        
        for event in events:
            ass_content += f"Dialogue: 0,{event['start']},{event['end']},Default,,0,0,0,,{event['text']}\n"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ass_content)

    def create_ass_subtitle_capcut(self, transcript, output_path: str, time_offset: float = 0):
        """Create ASS subtitle file with CapCut-style word-by-word highlighting"""
        ass_content = """[Script Info]
Title: Auto-generated captions
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,65,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,400,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        if hasattr(transcript, 'words') and transcript.words:
            words = transcript.words
            chunk_size = 4
            for i in range(0, len(words), chunk_size):
                chunk = words[i:i + chunk_size]
                if not chunk:
                    continue
                for j, current_word in enumerate(chunk):
                    word_start = current_word.start + time_offset
                    word_end = current_word.end + time_offset
                    text_parts = []
                    for k, w in enumerate(chunk):
                        word_text = self._attach_emoji(w.word.strip().upper())
                        if k == j:
                            text_parts.append(f"{{\\c&H00FFFF&}}{word_text}{{\\c&HFFFFFF&}}")
                        else:
                            text_parts.append(word_text)
                    text = " ".join(text_parts)
                    events.append({
                        'start': self.format_time(word_start),
                        'end': self.format_time(word_end),
                        'text': text
                    })
        elif hasattr(transcript, 'segments') and transcript.segments:
            for segment in transcript.segments:
                start = segment.get('start', 0) + time_offset
                end = segment.get('end', 0) + time_offset
                text = segment.get('text', '').strip().upper()
                if text:
                    events.append({
                        'start': self.format_time(start),
                        'end': self.format_time(end),
                        'text': text
                    })
        
        for event in events:
            ass_content += f"Dialogue: 0,{event['start']},{event['end']},Default,,0,0,0,,{event['text']}\n"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ass_content)
