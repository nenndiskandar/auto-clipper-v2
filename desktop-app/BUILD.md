# Build Instructions - Auto Clipper Desktop App

## Prerequisites

1. Python 3.10+
2. FFmpeg & yt-dlp (untuk bundling)

## Setup

```bash
cd desktop-app

# Install dependencies
pip install -r requirements.txt
pip install pyinstaller
```

## Build Single EXE

```bash
# Build dengan spec file
pyinstaller build.spec

# Output: dist/AutoClipper.exe
```

## Bundle FFmpeg & yt-dlp

Setelah build, copy file-file ini ke folder yang sama dengan exe:

```
dist/
├── AutoClipper.exe
├── ffmpeg/
│   ├── ffmpeg.exe      # Download dari https://ffmpeg.org/download.html
│   └── ffprobe.exe
└── yt-dlp.exe          # Download dari https://github.com/yt-dlp/yt-dlp/releases
```

### Download Links:
- FFmpeg: https://www.gyan.dev/ffmpeg/builds/ (pilih "ffmpeg-release-essentials.zip")
- yt-dlp: https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe

## Final Package

Zip folder `dist/` untuk distribusi:

```
AutoClipper-v1.0.zip
├── AutoClipper.exe     (~50MB)
├── ffmpeg/
│   ├── ffmpeg.exe      (~80MB)
│   └── ffprobe.exe
├── yt-dlp.exe          (~10MB)
└── README.txt
```

Total size: ~150MB

## Testing

```bash
# Run dari source (development)
python app.py

# Run built exe
dist/AutoClipper.exe
```

## Troubleshooting

### "FFmpeg not found"
- Pastikan ffmpeg.exe ada di folder `ffmpeg/` sejajar dengan exe

### "yt-dlp not found"  
- Pastikan yt-dlp.exe ada di folder yang sama dengan exe

### Antivirus false positive
- Ini normal untuk PyInstaller exe
- Add exception di antivirus atau sign exe dengan code signing certificate
