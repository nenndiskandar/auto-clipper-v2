# 📖 Panduan Pengguna YT-Short-Clipper

Panduan lengkap untuk menggunakan YT-Short-Clipper bagi pemula.

---

## 📑 Daftar Isi

- [1. Download & Instalasi](#1-download--instalasi)
  - [1.1 Download dari GitHub](#11-download-dari-github)
  - [1.2 Extract dan Jalankan](#12-extract-dan-jalankan)
- [2. Setup Library (yt-dlp, FFmpeg & Deno)](#2-setup-library-yt-dlp-ffmpeg--deno)
- [3. Setup Cookies YouTube](#3-setup-cookies-youtube)
  - [3.1 Install Extension Browser](#31-install-extension-browser)
  - [3.2 Export Cookies](#32-export-cookies)
  - [3.3 Upload Cookies ke Aplikasi](#33-upload-cookies-ke-aplikasi)
- [4. Konfigurasi AI API](#4-konfigurasi-ai-api)
  - [4.1 Buka AI API Settings](#41-buka-ai-api-settings)
  - [4.2 Pilih Modul AI](#42-pilih-modul-ai)
  - [4.3 Pilih AI Provider](#43-pilih-ai-provider)
  - [4.4 Masukkan API Key & Load Models](#44-masukkan-api-key--load-models)
  - [4.5 Validasi & Simpan](#45-validasi--simpan)
- [5. Mulai Menggunakan Aplikasi](#5-mulai-menggunakan-aplikasi)

---

## 1. Download & Instalasi

### 1.1 Download dari GitHub

1. Buka halaman GitHub YT-Short-Clipper
2. Klik menu **"Releases"** di sidebar kanan

   ![GitHub Releases](assets/docs/01.github-releases.png)

3. Pada halaman Releases, cari file dengan ekstensi `.exe` dan klik untuk download

   ![Download EXE](assets/docs/02.exe-link-download.png)

### 1.2 Jalankan Aplikasi

1. Setelah download selesai, double-click file `.exe` untuk menjalankan aplikasi
2. Jika muncul peringatan Windows Defender, klik **"More info"** → **"Run anyway"**

---

## 2. Setup Library (yt-dlp, FFmpeg & Deno)

Aplikasi membutuhkan library tambahan untuk download dan proses video.

1. Saat pertama kali membuka aplikasi, klik tombol **"Library"** di pojok kanan atas

   ![Library Button](assets/docs/03.library-button.png)

2. Klik tombol **"Download"** untuk mengunduh library yang diperlukan

   ![Download Library](assets/docs/04.download-library.png)

3. Tunggu proses download selesai

   ![Download Process](assets/docs/05.download-process.png)

4. Setelah selesai, status akan berubah menjadi ✅ **Installed**
5. **Restart aplikasi** setelah semua library terinstall

---

## 3. Setup Cookies YouTube

Cookies diperlukan agar aplikasi bisa mengakses video YouTube atas nama kamu.

### 3.1 Install Extension Browser

1. Buka browser Chrome/Edge
2. Install extension **"Get cookies.txt LOCALLY"**:
   - [Download untuk Chrome/Edge](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)

### 3.2 Export Cookies

1. Buka [youtube.com](https://youtube.com) dan **pastikan sudah login**
2. Klik icon extension di toolbar browser

   ![Get Cookies Locally](assets/docs/06.get-cookies-locally.png)

3. Klik **"Export"** untuk menyimpan cookies

   ![Export Text](assets/docs/07.export-text.png)

4. Simpan file sebagai `cookies.txt`

### 3.3 Upload Cookies ke Aplikasi

1. Di halaman utama aplikasi, klik tombol **"Upload Cookies"**

   ![Upload Cookies](assets/docs/08.upload-cookies.png)

2. Pilih file `cookies.txt` yang sudah di-export
3. Status cookies akan berubah menjadi ✅ **Valid**

> **⚠️ Penting:** 
> - Cookies YouTube biasanya expired dalam 1-2 minggu
> - Jika muncul error autentikasi, export ulang cookies dari browser
> - Jangan pernah share file cookies.txt ke orang lain

---

## 4. Konfigurasi AI API

Aplikasi membutuhkan API Key untuk mengakses layanan AI (GPT, Whisper, TTS).

### 4.1 Buka AI API Settings

1. Klik tombol **Settings** (⚙️) di pojok kanan atas
2. Pilih menu **"AI API Settings"**

   ![AI API Settings](assets/docs/09.ai-api-settings.png)

### 4.2 Pilih Modul AI

Aplikasi memiliki beberapa modul AI yang bisa dikonfigurasi secara terpisah:

   ![AI Setting Modules](assets/docs/10.ai-setting-modules.png)

- **Highlight Finder** - Mencari momen menarik dari video
- **Caption Maker** - Membuat caption/subtitle
- **Hook Maker** - Membuat hook text untuk intro
- **Title Generator** - Generate judul & deskripsi SEO

### 4.3 Pilih AI Provider

1. Klik dropdown **"AI Provider"**
2. Pilih provider yang kamu punya API key-nya:
   - **YT CLIP AI** - [https://ai.ytclip.org](https://ai.ytclip.org)
   - **OpenAI** - [https://platform.openai.com](https://platform.openai.com)
   - **Custom** - Pakai provider lain

   ![AI Provider Selector](assets/docs/11.ai-provider-selector.png)

3. URL akan otomatis terisi sesuai provider yang dipilih

### 4.4 Masukkan API Key & Load Models

1. Paste **API Key** kamu di field yang tersedia
2. Klik tombol **"Load Models"** untuk mengambil daftar model

   ![Load Model Button](assets/docs/12.load-model-button.png)

3. Pilih model yang ingin digunakan dari dropdown

   ![Select Models](assets/docs/13.select-models.png)

### 4.5 Validasi & Simpan

1. Klik tombol **"Validate"** untuk memastikan konfigurasi benar
2. Jika valid, klik **"Save"** untuk menyimpan

   ![Validate Configuration and Save](assets/docs/14.validate-configuration-and-save.png)

> **💡 Tips:** Ulangi langkah 4.2 - 4.5 untuk setiap modul AI yang ingin dikonfigurasi.

---

## 5. Mulai Menggunakan Aplikasi

Setelah semua setup selesai, kamu bisa mulai menggunakan aplikasi:

1. **Paste URL YouTube** yang ingin diproses
2. **Atur jumlah clips** yang diinginkan
3. **Klik "Start Processing"** dan tunggu hasilnya

Hasil clips akan tersimpan di folder `output/` dalam folder aplikasi.

---

## ❓ Butuh Bantuan?

Gabung [Discord Community](https://s.id/ytsdiscord) untuk tanya jawab, laporan bug, dan diskusi dengan pengguna lain.
