import asyncio
import os
import time
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message
import subprocess

# --- Konfigurasi ---
# Gunakan kredensial Anda dari my.telegram.org
API_ID = "YOUR_API_ID"
API_HASH = "YOUR_API_HASH"
BOT_TOKEN = "YOUR_BOT_TOKEN"

# Batas User (misal 1-2 orang saja agar kecepatan maksimal)
ALLOWED_USERS = [] # Masukkan User ID Telegram Anda ke list ini, misal [12345678, 87654321]

# Batasan Session Konkuren (supaya throttling tidak terjadi baik pada disk maupun bandwidth)
MAX_CONCURRENT_SESSIONS = 2
semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)

TEMP_DIR = Path("fast_temp")
TEMP_DIR.mkdir(exist_ok=True)

# Membuat Client Pyrogram dengan 4 workers yang cocok untuk 1-2 users.
# PENTING: Untuk kecepatan upload maksimum, instal pustaka 'tgcrypto' via pip.
app = Client(
    "ultra_fast_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=4
)

# Fungsi Download super cepat dengan Aria2
async def run_aria2c(url: str, output_path: Path):
    cmd = [
        "aria2c",
        "--enable-rpc=false",       # Matikan RPC untuk direktori script ini
        "-x", "32",                 # 32 koneksi per file
        "-s", "32",                 # 32 parts per file
        "-k", "1M",                 # 1 Megabyte minimal chunk size
        "--file-allocation=none",   # Sangat cepat untuk HDD/SSD modern
        "--min-split-size=1M",      # Minimal Split di 1MB
        "--max-connection-per-server=16",
        "--continue=true",          # Mendukung Resume Connection
        "-d", str(output_path.parent),
        "-o", output_path.name,
        url
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    # Tunggu proses selesai
    stdout, stderr = await process.communicate()
    
    if process.returncode == 0 and output_path.exists():
        return True
    return False

# Fungsi helper progress upload Pyrogram dengan throttling update pesan
async def progress_callback(current, total, message: Message, start_time: float, action: str):
    now = time.time()
    diff = now - start_time
    # Hindari division by zero
    if diff <= 0:
        return
        
    # Telegram limit update message itu 1 detik, lebih baik pakai selang 3-4 detik.
    if hasattr(progress_callback, "last_update") and (now - progress_callback.last_update) < 3.0:
        return
        
    progress_callback.last_update = now # type: ignore
    
    if total > 0:
        percent = current * 100 / total
        speed_bps = current / diff
        speed_mbps = speed_bps / (1024 * 1024)
        
        current_mb = current / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        
        text = (
            f"⏳ **{action}...**\n"
            f"Progress: {percent:.1f}%\n"
            f"Size: {current_mb:.1f} MB / {total_mb:.1f} MB\n"
            f"Speed: {speed_mbps:.1f} MB/s"
        )
        
        try:
            await message.edit_text(text)
        except Exception:
            pass

@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    await message.reply_text(
        "⚡️ **Fast Downloader & Uploader Bot**\n\n"
        "Kirim link HTTP/HTTPS, dan saya akan mendownload menggunakan `aria2c` "
        "lalu mengunggahnya dengan kecepatan maksimum.\n\n"
        "Khusus 1-2 pengguna dengan optimasi Pyrogram + tgCrypto!"
    )

@app.on_message(filters.text & ~filters.command("start"))
async def handle_url(client: Client, message: Message):
    # Pengecekan Private User
    if ALLOWED_USERS and message.from_user and message.from_user.id not in ALLOWED_USERS:
        await message.reply_text("⛔ Anda bukan pengguna VIP bot ini.", quote=True)
        return
        
    url = message.text.strip()
    if not url.startswith("http"):
        return

    # Memperoleh lock concurrency sehingga tidak menumpuk berlebihan
    async with semaphore:
        filename = f"{int(time.time())}.mp4"
        output_path = TEMP_DIR / filename
        
        progress_msg = await message.reply_text("🔍 Mulai inisiasi downloader...")
        
        try:
            # 1. TAHAP DOWNLOAD (Aria2c)
            await progress_msg.edit_text("⬇️ Mendownload... (32 Connections)")
            start_time_dl = time.time()
            
            success = await run_aria2c(url, output_path)
            if not success:
                await progress_msg.edit_text("❌ Download gagal via Aria2.")
                return
                
            elapsed_dl = time.time() - start_time_dl
            file_size_mb = output_path.stat().st_size / (1024*1024)
            speed_dl = file_size_mb / elapsed_dl if elapsed_dl > 0 else 0
            
            # 2. TAHAP UPLOAD (Pyrogram)
            await progress_msg.edit_text(
                f"✅ **Download Selesai!**\n"
                f"Ukuran: {file_size_mb:.1f} MB\n"
                f"Kecepatan: {speed_dl:.1f} MB/s\n\n"
                f"⬆️ Memulai Upload ke server Telegram..."
            )
            
            start_time_up = time.time()
            progress_callback.last_update = 0 # type: ignore
            
            await client.send_document(
                chat_id=message.chat.id,
                document=str(output_path),
                caption=f"⬇️ **Speed DL**: {speed_dl:.1f} MB/s",
                progress=progress_callback,
                progress_args=(progress_msg, start_time_up, "Uploading")
            )
            
            elapsed_up = time.time() - start_time_up
            speed_up = file_size_mb / elapsed_up if elapsed_up > 0 else 0
            
            await progress_msg.edit_text(
                f"🎉 **File Berhasil Diproses!**\n\n"
                f"⬇️ **Kecepatan Download:** {speed_dl:.1f} MB/s\n"
                f"⬆️ **Kecepatan Upload:** {speed_up:.1f} MB/s\n\n"
                f"_(Cleanup dilakukan pada file lokal otomatis)_"
            )
            
        except Exception as e:
            await progress_msg.edit_text(f"❌ Terjadi kesalahan: {e}")
            
        finally:
            # 3. TAHAP CLEANUP
            if output_path.exists():
                try:
                    os.remove(output_path)
                    print(f"🗑 Cleanup successful: {output_path.name}")
                except:
                    pass

if __name__ == "__main__":
    print("🚀 Bot Ultra Fast sedang berjalan!")
    print("💡 Catatan: pastikan 'tgcrypto' dan 'aria2' sudah terinstall agar dapat mencapai kecepatan maksimum.")
    app.run()
