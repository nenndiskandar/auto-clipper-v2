# Debug Mode

## Console Logging

Ketika menjalankan aplikasi dari terminal (bukan dari .exe), aplikasi akan otomatis menampilkan log debug ke console.

### Cara menggunakan:

```bash
# Jalankan dari terminal
cd desktop-app
python app.py
```

Semua log akan muncul di console dengan prefix `[DEBUG]`, termasuk:
- Status processing
- Error messages lengkap
- FFmpeg output
- File paths yang digunakan

### Build mode (EXE)

Ketika aplikasi di-build menjadi .exe, console logging akan otomatis dinonaktifkan untuk pengalaman user yang lebih bersih.

## Error Handling

Error dari FFmpeg sekarang ditangani dengan lebih baik:
- Hanya menampilkan error message yang relevan (bukan version info)
- Fallback otomatis jika concat demuxer gagal
- Validasi file output sebelum melanjutkan ke step berikutnya

## Troubleshooting

Jika ada error saat processing:
1. Jalankan dari terminal dengan `python app.py`
2. Lihat log lengkap di console
3. Check folder output untuk file temporary yang mungkin tertinggal
4. Pastikan ffmpeg terinstall dengan benar: `ffmpeg -version`
