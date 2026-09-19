import os
import re

class SubtitleGeneratorMixin:
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
                parts.append("{\\kf%d}%s" % (dur_cs, str(w.word).strip().upper()))
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
                        word_text = w.word.strip().upper()
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
