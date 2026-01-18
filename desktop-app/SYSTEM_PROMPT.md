# System Prompt Customization

## Overview

System prompt adalah instruksi yang diberikan ke AI untuk mencari highlight dari video. Anda bisa customize prompt ini sesuai kebutuhan konten Anda.

## Cara Edit System Prompt

1. Buka **Settings** (⚙️)
2. Pilih tab **OpenAI API**
3. Scroll ke bawah ke section **System Prompt**
4. Edit prompt sesuai kebutuhan
5. Klik **Save Settings**

## Placeholders Wajib

System prompt harus mengandung 3 placeholders berikut:

- `{num_clips}` - Jumlah clip yang diminta (otomatis ditambah 3 untuk buffer)
- `{video_context}` - Info video (judul, channel, deskripsi)
- `{transcript}` - Transcript lengkap dengan timestamp

## Contoh Custom Prompt

### Untuk Konten Gaming:
```
Kamu adalah editor video gaming profesional. Dari transcript gameplay berikut, pilih {num_clips} momen paling epic untuk dijadikan highlight shorts.

{video_context}

Kriteria momen yang bagus:
- Clutch play atau comeback
- Momen lucu atau fail
- Skill showcase atau trickshot
- Interaksi chat yang menarik
- Rage moment atau reaksi emosional

⚠️ DURASI: 60-120 detik per clip

⚠️ HOOK TEXT:
Buat hook yang hype dan catchy (max 15 kata, bahasa gaul gaming)

Transcript:
{transcript}

Return JSON array:
[
  {{
    "start_time": "00:01:23,000",
    "end_time": "00:02:15,000",
    "title": "Judul singkat",
    "reason": "Alasan kenapa menarik",
    "hook_text": "Hook yang catchy"
  }}
]

Return HANYA JSON array.
```

### Untuk Konten Edukasi:
```
Kamu adalah editor video edukasi. Dari transcript berikut, pilih {num_clips} segment paling informatif dan mudah dipahami.

{video_context}

Kriteria segment yang bagus:
- Penjelasan konsep yang clear
- Ada contoh atau analogi menarik
- Tips atau insight praktis
- Momen "aha!" atau revelasi
- Topik yang self-contained (tidak butuh context tambahan)

⚠️ DURASI: 60-120 detik per clip

⚠️ HOOK TEXT:
Buat hook yang curiosity-driven (max 15 kata, formal tapi engaging)

Transcript:
{transcript}

Return JSON array:
[
  {{
    "start_time": "00:01:23,000",
    "end_time": "00:02:15,000",
    "title": "Judul singkat",
    "reason": "Alasan kenapa menarik",
    "hook_text": "Hook yang catchy"
  }}
]

Return HANYA JSON array.
```

## Tips

1. **Jelas dan Spesifik**: Jelaskan dengan detail kriteria highlight yang Anda inginkan
2. **Sesuaikan Bahasa**: Gunakan bahasa yang sesuai dengan target audience (formal/casual/gaul)
3. **Durasi Penting**: Pastikan tetap ada aturan durasi 60-120 detik
4. **Hook Text**: Sesuaikan style hook dengan platform target (TikTok/Reels/Shorts)
5. **Test & Iterate**: Coba beberapa variasi prompt untuk hasil terbaik

## Reset ke Default

Jika ingin kembali ke prompt default, klik tombol **🔄 Reset to Default** di settings.

## Troubleshooting

**AI tidak mengikuti instruksi:**
- Pastikan instruksi jelas dan tidak ambigu
- Coba tambahkan contoh konkret
- Gunakan format bullet points untuk kriteria

**Hasil tidak konsisten:**
- Tambahkan lebih banyak constraint
- Gunakan kata "WAJIB" atau "HARUS" untuk aturan penting
- Spesifikasikan format output dengan jelas

**Durasi clip tidak sesuai:**
- Pastikan ada section khusus untuk aturan durasi
- Gunakan emoji ⚠️ untuk menekankan pentingnya
- Tambahkan contoh durasi yang diinginkan
