# Telegram Downloader Bot - Multi-User Support

Bot Telegram canggih untuk mendownload video dari berbagai sumber (JSON streaming, HLS/M3U8, MP4) dengan dukungan multi-user dan akselerasi download.

## Fitur Utama
- **Multi-User**: Mendukung hingga 2 user download sekaligus secara bersamaan (Semaphore).
- **Engine Tercepat**: Menggunakan **aria2c** turbo (16 koneksi), **yt-dlp**, dan fallback streaming.
- **Auto-Parsing**: Mendeteksi otomatis link video dan subtitle dari file JSON (Dramaflickreels, dll).
- **HLS Optimization**: Download stream M3U8 dengan opsi resolusi dan burn subtitle.
- **Session Isolate**: Folder download terpisah per user (`downloads/user_id/`).
- **Progress Bar**: Tampilan progress download yang cantik dan real-time.

---

## Cara Pasang (Installation)

### 1. Persyaratan Sistem (Prerequisites)
Pastikan Anda sudah menginstal software berikut di server/PC Anda:
- **Python 3.10+**
- **Git**
- **aria2c** (Wajib untuk kecepatan turbo)
- **FFmpeg** (Wajib untuk burn subtitle/merge video)
- **yt-dlp** (Fallback download)

#### Di Windows:
Instal `aria2` dan `ffmpeg` secara manual dan tambahkan ke `Path` sistem, atau gunakan **Chocolatey**:
```powershell
choco install aria2 ffmpeg yt-dlp
```

#### Di Linux (Ubuntu/Debian):
```bash
sudo apt update
sudo apt install aria2 ffmpeg python3-pip git -y
sudo pip3 install yt-dlp
```

### 2. Clone Repositori
```bash
git clone https://github.com/XIAOYU9977/telegram-downloader-bot.git
cd telegram-downloader-bot
```

### 3. Instal Dependensi Python
```bash
pip install -r requirements.txt
```

### 4. Konfigurasi
Buka file `config.py` dan isi token serta API Anda:
- `BOT_TOKEN`: Token dari [@BotFather](https://t.me/BotFather).
- `API_ID` & `API_HASH`: Dapatkan dari [my.telegram.org](https://my.telegram.org) (Hanya jika menggunakan Pyrogram untuk upload cepat).
- `ALLOWED_USERS`: Daftar ID Telegram yang diizinkan menggunakan bot.

### 5. Jalankan Bot
```bash
python bot.py
```

---

## Perintah Bot (Commands)
- `/start`: Menampilkan menu pengaturan (Kualitas & Format).
- `/l [judul] [link]`: Download video tunggal.
- `/batch [judul] [link1] [link2] ...`: Batch download banyak link.
- `/cancel`: Membatalkan semua tugas download user saat ini.

## Lisensi
Proyek ini dibuat untuk tujuan edukasi.
