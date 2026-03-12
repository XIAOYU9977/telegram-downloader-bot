import asyncio
import uuid
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Tuple, Optional, List
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)
import aiofiles

from config import (
    BOT_TOKEN, DOWNLOAD_DIR, ALLOWED_USERS, DELETE_AFTER_UPLOAD,
    MAX_CONCURRENT_DOWNLOADS, SESSION_TIMEOUT,
    DOWNLOAD_TIMEOUT, PROCESSING_TIMEOUT, UPLOAD_TIMEOUT, MAX_RETRIES,
    CLEANUP_DELAY, CLEANUP_ON_ERROR
)
from downloader import DownloadManager
from processor import VideoProcessor
from uploader import TelegramUploader
from session import SessionManager
from utils import (
    JSONParser, SubtitleDetector, FileCleanup,
    cleanup_file, format_size, logger
)

# Conversation states
AWAITING_CONFIRMATION = 0
AWAITING_BATCH_CHOICE = 1

class DownloaderBot:
    def __init__(self):
        self.download_manager = DownloadManager()
        self.video_processor = VideoProcessor()
        self.uploader = None
        self.session_manager = SessionManager()
        self.cleanup = FileCleanup()
        
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        await update.message.reply_text(
            "🎬 <b>Video Downloader Bot</b>\n\n"
            "Kirimkan saya file <b>.json</b> untuk mulai mendownload video.\n\n"
            "Fitur:\n"
            "✅ Deteksi semua episode dari JSON\n"
            "✅ Pilih download 1 episode atau semua episode\n"
            "✅ Auto detect subtitle Indonesia\n"
            "✅ Hard subtitle otomatis\n"
            "✅ Auto hapus file setelah selesai\n"
            "✅ Auto hapus session jika terjadi error\n\n"
            "Cara kerja:\n"
            "1️⃣ Kirim file JSON\n"
            "2️⃣ Bot akan membaca semua episode & subtitle\n"
            "3️⃣ Pilih download 1 atau semua episode\n"
            "4️⃣ Konfirmasi dengan OK\n"
            "5️⃣ Bot download & proses\n"
            "6️⃣ Video dikirim ke Anda\n"
            "7️⃣ File otomatis dihapus\n\n"
            "Gunakan /help untuk bantuan lebih lanjut.",
            parse_mode="HTML"
        )
    
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        await update.message.reply_text(
            "📖 <b>Bantuan</b>\n\n"
            "<b>Format JSON yang didukung:</b>\n"
            "• dramabox (v1 & v2)\n"
            "• dramawave\n"
            "• flikreels\n"
            "• goodshort\n"
            "• freereels\n"
            "• stardust\n"
            "• vigloo\n"
            "• meloshort\n\n"
            "<b>Fitur Subtitle Indonesia:</b>\n"
            "• Auto detect semua variasi kode: id, ind, indonesia, bahasa, dll\n"
            "• Hard subtitle otomatis jika ditemukan\n"
            "• Konversi VTT/SRT ke ASS untuk kualitas terbaik\n\n"
            "<b>Auto Cleanup:</b>\n"
            "• Video dihapus setelah upload\n"
            "• Subtitle dihapus setelah diproses\n"
            "• File JSON dihapus setelah selesai\n"
            "• File error otomatis dihapus\n"
            "• Session otomatis dihapus jika error\n\n"
            "<b>Langkah-langkah:</b>\n"
            "1. Kirim file .json\n"
            "2. Bot akan menampilkan daftar episode\n"
            "3. Pilih nomor episode atau 'SEMUA'\n"
            "4. Ketik OK untuk konfirmasi\n"
            "5. Tunggu proses selesai\n\n"
            "Gunakan /cancel untuk membatalkan proses.",
            parse_mode="HTML"
        )
    
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel current operation and cleanup files"""
        user_id = update.effective_user.id
        await self._cleanup_user_session(user_id, context, "dibatalkan oleh user")
        await update.message.reply_text("✅ Proses dibatalkan. File dibersihkan.")
        return ConversationHandler.END
    
    async def _cleanup_user_session(self, user_id: int, context: ContextTypes.DEFAULT_TYPE, reason: str = ""):
        """
        Cleanup SEMUA file dan session untuk user — aggressive mode.
        Scan seluruh DOWNLOAD_DIR untuk file milik user.
        """
        logger.info(f"🧹 Membersihkan session untuk user {user_id}. Alasan: {reason}")
        
        files_to_cleanup = []
        
        # Ambil session jika ada
        session = self.session_manager.get_session(user_id)
        
        # Cleanup JSON file dari session
        if session and session.json_file_path:
            json_path = Path(session.json_file_path)
            if json_path.exists():
                files_to_cleanup.append(json_path)
                logger.info(f"  - Akan hapus JSON: {json_path.name}")
        
        # Cleanup file dari context user_data
        if context and context.user_data:
            file_keys = ['video_path', 'subtitle_path', 'output_path', 'json_path']
            for key in file_keys:
                if key in context.user_data:
                    path = Path(context.user_data[key])
                    if path.exists():
                        files_to_cleanup.append(path)
                        logger.info(f"  - Akan hapus {key}: {path.name}")
            
            # Hapus semua data user
            context.user_data.clear()
        
        # ── Aggressive scan: hapus SEMUA file milik user di DOWNLOAD_DIR ──────
        try:
            for f in DOWNLOAD_DIR.glob(f"{user_id}_*"):
                if f.is_file() and f not in files_to_cleanup:
                    files_to_cleanup.append(f)
                    logger.info(f"  - Akan hapus (scan): {f.name}")
        except Exception as e:
            logger.warning(f"  - Scan DOWNLOAD_DIR gagal: {e}")
        
        # Hapus session
        self.session_manager.delete_session(user_id)
        
        # Hapus file-file
        if files_to_cleanup:
            await FileCleanup.cleanup_batch_files(files_to_cleanup, delay=0)
            logger.info(f"✅ {len(files_to_cleanup)} file dibersihkan untuk user {user_id}")
        else:
            logger.info(f"✅ Tidak ada file untuk dibersihkan untuk user {user_id}")
    
    def extract_title_episode(self, data: Dict[str, Any], filename: str = "") -> Tuple[str, str, bool]:
        """Extract title, episode, and subtitle availability from JSON data"""
        title = None
        episode = None
        has_subtitle = False
        
        try:
            # Dramabox v2 format
            if "data" in data and "bookName" in data["data"]:
                title = data["data"]["bookName"]
                if "episodes" in data["data"] and len(data["data"]["episodes"]) > 0:
                    first_ep = data["data"]["episodes"][0]
                    chapter_index = first_ep.get("chapterIndex", 0)
                    episode = str(chapter_index + 1)
                    
                    # Cek subtitle
                    if "subtitles" in first_ep and len(first_ep["subtitles"]) > 0:
                        for sub in first_ep["subtitles"]:
                            if SubtitleDetector.is_indonesian_subtitle(sub):
                                has_subtitle = True
                                break
                    return title, episode, has_subtitle
            
            # GoodShort format (dengan array videos)
            if "title" in data and "videos" in data:
                title = data.get("title", "Video")
                if data.get("videos") and len(data["videos"]) > 0:
                    first_video = data["videos"][0]
                    episode = str(first_video.get("episode", "1"))
                    
                    # GoodShort biasanya tidak punya subtitle di JSON ini
                    has_subtitle = False
                    return title, episode, has_subtitle
            
            # Extract from filename as fallback
            if filename:
                ep_match = re.search(r'[Ee]p?(?:isode)?[\s._-]*(\d+)', filename)
                if ep_match:
                    episode = ep_match.group(1)
                
                name_without_ext = Path(filename).stem
                if ep_match:
                    title = re.sub(r'[Ee]p?(?:isode)?[\s._-]*\d+', '', name_without_ext).strip(' ._-')
                else:
                    title = name_without_ext
            
            # Dramawave format
            if "data" in data:
                if "info" in data["data"] and "episode_list" in data["data"]["info"]:
                    info = data["data"]["info"]
                    if "name" in info:
                        title = info["name"]
                    
                    first_ep = data["data"]["info"]["episode_list"][0]
                    if "subtitle_list" in first_ep:
                        for sub in first_ep["subtitle_list"]:
                            if SubtitleDetector.is_indonesian_subtitle(sub):
                                has_subtitle = True
                                break
                
                elif "drama_title" in data["data"]:
                    title = data["data"]["drama_title"]
                    if "sublist" in data["data"] and len(data["data"]["sublist"]) > 0:
                        for sub in data["data"]["sublist"]:
                            if SubtitleDetector.is_indonesian_subtitle(sub):
                                has_subtitle = True
                                break
            
            elif "episode_list" in data and len(data["episode_list"]) > 0:
                first_ep = data["episode_list"][0]
                if "name" in data:
                    title = data["name"]
                if "subtitle_list" in first_ep:
                    for sub in first_ep["subtitle_list"]:
                        if SubtitleDetector.is_indonesian_subtitle(sub):
                            has_subtitle = True
                            break
            
            if not title:
                for field in ["title", "name", "drama_title", "bookName"]:
                    if field in data:
                        title = data[field]
                        break
                    elif isinstance(data.get("data"), dict) and field in data["data"]:
                        title = data["data"][field]
                        break
            
            if not episode:
                for field in ["episode", "chapter", "index", "chapterIndex", "episode_num"]:
                    if field in data:
                        episode = str(data[field])
                        if field == "chapterIndex":
                            episode = str(int(episode) + 1)  # Convert 0-based to 1-based
                        break
                    elif isinstance(data.get("data"), dict) and field in data["data"]:
                        episode = str(data["data"][field])
                        if field == "chapterIndex":
                            episode = str(int(episode) + 1)
                        break
            
            if title:
                title = re.sub(r'\s*[-\|]\s*(EP?\d+|Episode\s*\d+).*$', '', title, flags=re.I)
                title = title.strip()
            
        except Exception as e:
            logger.error(f"Error extracting title/episode: {e}")
        
        return title or "Video", episode or "1", has_subtitle
    
    async def handle_json_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle JSON file upload - extract all episodes and ask for choice"""
        user_id = update.effective_user.id
        
        if ALLOWED_USERS and user_id not in ALLOWED_USERS:
            await update.message.reply_text("⛔ Anda tidak diizinkan menggunakan bot ini.")
            return ConversationHandler.END
        
        # Cek session aktif
        if self.session_manager.has_active_session(user_id):
            await update.message.reply_text(
                "⚠️ Masih ada proses yang berjalan. Harap tunggu hingga selesai.\n"
                "Ketik /cancel untuk membatalkan proses sebelumnya."
            )
            return ConversationHandler.END
        
        json_path = None
        try:
            file = await update.message.document.get_file()
            filename = update.message.document.file_name or ""
            json_path = DOWNLOAD_DIR / f"{user_id}_{uuid.uuid4()}.json"
            await file.download_to_drive(json_path)
            
            data = await JSONParser.parse_json_file(json_path)
            if not data:
                await update.message.reply_text("❌ File JSON tidak valid.")
                if json_path and json_path.exists():
                    await FileCleanup.safe_delete(json_path)
                return ConversationHandler.END
            
            # Buat session baru
            self.session_manager.create_session(user_id, data, str(json_path))
            
            video_url, subtitle_url = JSONParser.extract_video_url(data)
            
            if subtitle_url:
                logger.info(f"Indonesian subtitle detected: {subtitle_url[:100]}...")
            
            if not video_url:
                await update.message.reply_text(
                    "❌ Tidak dapat menemukan URL video dalam file JSON.\n"
                    "Pastikan format JSON didukung."
                )
                await self._cleanup_user_session(user_id, context, "tidak ada URL video")
                return ConversationHandler.END
            
            all_episodes = JSONParser.extract_all_episodes(data)
            
            if not all_episodes:
                title, episode, has_subtitle = self.extract_title_episode(data, filename)
                
                context.user_data['episodes'] = [{
                    "episode": episode,
                    "title": title,
                    "url": video_url,
                    "subtitle_url": subtitle_url
                }]
                context.user_data['total_episodes'] = 1
                context.user_data['drama_title'] = title
                context.user_data['json_path'] = str(json_path)
                
                subtitle_text = "Ya" if subtitle_url else "Tidak"
                confirmation_text = (
                    "📋 <b>Konfirmasi Data</b>\n\n"
                    f"Judul: {title}\n"
                    f"Episode: {episode}\n"
                    f"Subtitle Indonesia: {subtitle_text}\n\n"
                    "Ketik:\n"
                    "<b>OK</b> → untuk mulai proses\n"
                    "<b>BATAL</b> → untuk membatalkan\n\n"
                    "<i>File akan otomatis dihapus setelah selesai</i>"
                )
                
                await update.message.reply_text(confirmation_text, parse_mode="HTML")
                return AWAITING_CONFIRMATION
            
            else:
                drama_title, _, _ = self.extract_title_episode(data, filename)
                
                context.user_data['episodes'] = all_episodes
                context.user_data['total_episodes'] = len(all_episodes)
                context.user_data['drama_title'] = drama_title
                context.user_data['json_path'] = str(json_path)
                
                eps_with_sub = sum(1 for ep in all_episodes if ep.get('subtitle_url'))
                
                episode_list = "\n".join([
                    f"• Episode {ep['episode']}: {'[SUB] ' if ep.get('subtitle_url') else ''}{ep['title'][:30]}"
                    for ep in all_episodes[:10]
                ])
                
                if len(all_episodes) > 10:
                    episode_list += f"\n• ... dan {len(all_episodes) - 10} episode lainnya"
                
                choice_text = (
                    f"📋 <b>Ditemukan {len(all_episodes)} Episode</b>\n\n"
                    f"Judul: {drama_title}\n"
                    f"Subtitle Indonesia: {eps_with_sub} episode\n\n"
                    f"Daftar Episode:\n{episode_list}\n\n"
                    "📝 <b>Pilihan Download:</b>\n\n"
                    "Ketik nomor episode (contoh: 1, 1-5, 1,3,5)\n"
                    "Atau ketik <b>SEMUA</b> untuk download semua episode\n"
                    "Ketik <b>BATAL</b> untuk membatalkan\n\n"
                    "<i>File akan otomatis dihapus setelah selesai</i>"
                )
                
                await update.message.reply_text(choice_text, parse_mode="HTML")
                return AWAITING_BATCH_CHOICE
            
        except Exception as e:
            logger.error(f"Error handling JSON file: {e}")
            await self._cleanup_user_session(user_id, context, f"error: {str(e)}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
            return ConversationHandler.END
    
    async def handle_batch_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle user's choice for batch download"""
        user_id = update.effective_user.id
        choice = update.message.text.strip().upper()
        
        if choice == "BATAL":
            await self._cleanup_user_session(user_id, context, "dibatalkan user")
            await update.message.reply_text("✅ Proses dibatalkan.")
            return ConversationHandler.END
        
        episodes = context.user_data.get('episodes', [])
        
        if not episodes:
            await update.message.reply_text("❌ Data episode tidak ditemukan.")
            await self._cleanup_user_session(user_id, context, "data episode kosong")
            return ConversationHandler.END
        
        if choice == "SEMUA":
            selected_episodes = episodes
            context.user_data['selected_episodes'] = selected_episodes
            context.user_data['is_batch'] = True
            
            eps_with_sub = sum(1 for ep in selected_episodes if ep.get('subtitle_url'))
            confirmation_text = (
                "📋 <b>Konfirmasi Batch Download</b>\n\n"
                f"Judul: {context.user_data.get('drama_title', 'Video')}\n"
                f"Total Episode: {len(selected_episodes)}\n"
                f"Subtitle Indonesia: {eps_with_sub} episode\n\n"
                "Ketik:\n"
                "<b>OK</b> → untuk mulai download semua episode\n"
                "<b>BATAL</b> → untuk membatalkan\n\n"
                "<i>File akan otomatis dihapus setelah selesai</i>"
            )
            
            await update.message.reply_text(confirmation_text, parse_mode="HTML")
            return AWAITING_CONFIRMATION
        
        try:
            selected_numbers = set()
            parts = choice.replace(' ', '').split(',')
            
            for part in parts:
                if '-' in part:
                    start, end = map(int, part.split('-'))
                    if start > end:
                        raise ValueError("Range tidak valid")
                    selected_numbers.update(range(start, end + 1))
                else:
                    selected_numbers.add(int(part))
            
            # Validasi nomor episode
            max_episode = max(int(ep['episode']) if ep['episode'].isdigit() else 0 for ep in episodes)
            for num in selected_numbers:
                if num < 1 or num > max_episode:
                    await update.message.reply_text(
                        f"❌ Nomor episode {num} tidak valid. Hanya ada episode 1-{max_episode}."
                    )
                    return AWAITING_BATCH_CHOICE
            
            selected_episodes = []
            for ep in episodes:
                try:
                    ep_num = int(ep['episode'])
                    if ep_num in selected_numbers:
                        selected_episodes.append(ep)
                except:
                    if ep['episode'] in [str(n) for n in selected_numbers]:
                        selected_episodes.append(ep)
            
            if not selected_episodes:
                await update.message.reply_text(
                    "❌ Tidak ada episode yang sesuai. Silakan coba lagi.\n"
                    "Contoh: 1, 1-5, 1,3,5"
                )
                return AWAITING_BATCH_CHOICE
            
            context.user_data['selected_episodes'] = selected_episodes
            context.user_data['is_batch'] = len(selected_episodes) > 1
            
            episode_list = ", ".join([ep['episode'] for ep in selected_episodes])
            eps_with_sub = sum(1 for ep in selected_episodes if ep.get('subtitle_url'))
            confirmation_text = (
                "📋 <b>Konfirmasi Download</b>\n\n"
                f"Judul: {context.user_data.get('drama_title', 'Video')}\n"
                f"Episode: {episode_list}\n"
                f"Total: {len(selected_episodes)} episode\n"
                f"Subtitle Indonesia: {eps_with_sub} episode\n\n"
                "Ketik:\n"
                "<b>OK</b> → untuk mulai proses\n"
                "<b>BATAL</b> → untuk membatalkan\n\n"
                "<i>File akan otomatis dihapus setelah selesai</i>"
            )
            
            await update.message.reply_text(confirmation_text, parse_mode="HTML")
            return AWAITING_CONFIRMATION
            
        except ValueError as e:
            logger.error(f"Error parsing batch choice: {e}")
            await update.message.reply_text(
                "❌ Format tidak valid. Gunakan format:\n"
                "• Nomor episode: 1\n"
                "• Beberapa episode: 1,3,5\n"
                "• Range episode: 1-5\n"
                "• SEMUA untuk semua episode"
            )
            return AWAITING_BATCH_CHOICE
        except Exception as e:
            logger.error(f"Unexpected error parsing batch choice: {e}")
            await self._cleanup_user_session(user_id, context, f"error parsing: {str(e)}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
            return ConversationHandler.END
    
    async def process_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle confirmation and start processing (single or batch)"""
        user_id = update.effective_user.id
        confirmation = update.message.text.strip().upper()
        
        if confirmation == "BATAL":
            await self._cleanup_user_session(user_id, context, "dibatalkan user")
            await update.message.reply_text("✅ Proses dibatalkan.")
            return ConversationHandler.END
        
        if confirmation != "OK":
            await update.message.reply_text("Ketik OK untuk konfirmasi atau BATAL untuk membatalkan:")
            return AWAITING_CONFIRMATION
        
        selected_episodes = context.user_data.get('selected_episodes', [])
        if not selected_episodes:
            episodes = context.user_data.get('episodes', [])
            if episodes and len(episodes) == 1:
                selected_episodes = episodes
            else:
                await update.message.reply_text("❌ Tidak ada episode yang dipilih.")
                await self._cleanup_user_session(user_id, context, "tidak ada episode dipilih")
                return ConversationHandler.END
        
        drama_title = context.user_data.get('drama_title', 'Video')
        json_path = Path(context.user_data.get('json_path', ''))
        is_batch = len(selected_episodes) > 1
        
        status_msg = await update.message.reply_text(
            f"🚀 Memulai proses download {len(selected_episodes)} episode...\n"
            f"<i>File akan otomatis dihapus setelah selesai</i>",
            parse_mode="HTML"
        )
        
        if not self.uploader:
            self.uploader = TelegramUploader(context.bot)
        
        successful = 0
        failed = 0
        all_files_to_cleanup = []
        
        try:
            for idx, episode_data in enumerate(selected_episodes, 1):
                episode_num = episode_data['episode']
                episode_title = episode_data['title']
                video_url = episode_data['url']
                subtitle_url = episode_data.get('subtitle_url')
                
                try:
                    if is_batch:
                        await self.uploader.update_message(
                            user_id,
                            status_msg.message_id,
                            f"📥 <b>Downloading episode {idx}/{len(selected_episodes)}</b>\n"
                            f"Episode: {episode_num}"
                        )
                except:
                    pass
                
                safe_title = "".join(c for c in drama_title if c.isalnum() or c in ' ._-')[:30]
                safe_episode = f"EP{episode_num.zfill(2) if episode_num.isdigit() else episode_num}"
                video_path = DOWNLOAD_DIR / f"{user_id}_{safe_title}_{safe_episode}.mp4"
                subtitle_path = DOWNLOAD_DIR / f"{user_id}_{safe_title}_{safe_episode}.srt"
                output_path = DOWNLOAD_DIR / f"{user_id}_{safe_title}_{safe_episode}_sub.mp4"
                
                episode_files = [video_path, subtitle_path, output_path]
                all_files_to_cleanup.extend(episode_files)
                
                try:
                    async def video_progress(current):
                        if not is_batch:
                            try:
                                await self.uploader.update_message(
                                    user_id,
                                    status_msg.message_id,
                                    f"⬇️ <b>Downloading Episode {episode_num}...</b>\n"
                                    f"Size: {format_size(current)}"
                                )
                            except:
                                pass
                    
                    downloaded_video = await asyncio.wait_for(
                        self.download_manager.download_video(
                            video_url, video_path, video_progress if not is_batch else None
                        ),
                        timeout=DOWNLOAD_TIMEOUT
                    )
                    
                    if not downloaded_video:
                        failed += 1
                        logger.error(f"Download failed for episode {episode_num}")
                        # Hapus file untuk episode ini
                        await FileCleanup.cleanup_episode_files(
                            video_path=video_path if video_path.exists() else None,
                            subtitle_path=subtitle_path if subtitle_path.exists() else None,
                            output_path=output_path if output_path.exists() else None,
                            delay=2
                        )
                        continue
                        
                except asyncio.TimeoutError:
                    logger.error(f"Download timeout for episode {episode_num}")
                    failed += 1
                    await FileCleanup.cleanup_episode_files(
                        video_path=video_path if video_path.exists() else None,
                        delay=2
                    )
                    continue
                except Exception as e:
                    logger.error(f"Download error for episode {episode_num}: {e}")
                    failed += 1
                    await FileCleanup.cleanup_episode_files(
                        video_path=video_path if video_path.exists() else None,
                        delay=2
                    )
                    continue
                
                subtitle_file = None
                if subtitle_url:
                    try:
                        await self.uploader.update_message(
                            user_id,
                            status_msg.message_id,
                            f"⬇️ <b>Downloading Indonesian subtitle for Episode {episode_num}...</b>"
                        )
                        
                        subtitle_file = await self.download_manager.download_subtitle(
                            subtitle_url, subtitle_path
                        )
                        
                        if subtitle_file:
                            logger.info(f"Indonesian subtitle downloaded for episode {episode_num}")
                    except Exception as e:
                        logger.warning(f"Subtitle download failed: {e}")
                        # Hapus subtitle file jika gagal
                        if subtitle_path.exists():
                            await FileCleanup.safe_delete(subtitle_path)
                
                final_video = downloaded_video
                if subtitle_file:
                    try:
                        await self.uploader.update_message(
                            user_id,
                            status_msg.message_id,
                            f"🎬 <b>Burning Indonesian subtitle for Episode {episode_num}...</b>"
                        )
                        
                        async def burn_progress(current):
                            if not is_batch:
                                try:
                                    await self.uploader.update_message(
                                        user_id,
                                        status_msg.message_id,
                                        f"🎬 <b>Processing Episode {episode_num}...</b>\n"
                                        f"Size: {format_size(current)}"
                                    )
                                except:
                                    pass
                        
                        processed_video = await asyncio.wait_for(
                            self.video_processor.burn_subtitle(
                                downloaded_video, subtitle_file, output_path, burn_progress, "id"
                            ),
                            timeout=PROCESSING_TIMEOUT
                        )
                        
                        if processed_video:
                            final_video = processed_video
                            logger.info(f"Subtitle burned for episode {episode_num}")
                        else:
                            logger.warning(f"Subtitle burning failed, using original video")
                            final_video = downloaded_video
                    except Exception as e:
                        logger.warning(f"Subtitle burning failed: {e}")
                        final_video = downloaded_video
                
                try:
                    if is_batch:
                        async def batch_progress(current, total):
                            try:
                                pct = (current / total) * 100
                                await self.uploader.update_message(
                                    user_id, status_msg.message_id,
                                    f"📤 <b>Uploading Ep {episode_num}</b>"
                                    f" ({idx}/{len(selected_episodes)})\n"
                                    f"Progress: {pct:.0f}%\n"
                                    f"Size: {format_size(current)}/{format_size(total)}"
                                )
                            except Exception:
                                pass

                        upload_ok = await self.uploader.upload_video(
                            file_path=final_video,
                            chat_id=user_id,
                            title=drama_title,
                            episode=episode_num,
                            progress_callback=batch_progress,
                        )

                        if not upload_ok:
                            failed += 1
                            logger.error(f"Upload gagal ep {episode_num}")
                            continue
                        
                        try:
                            await self.uploader.update_message(
                                user_id,
                                status_msg.message_id,
                                f"✅ <b>Progress: {idx}/{len(selected_episodes)}</b>\n"
                                f"Episode {episode_num} selesai"
                            )
                        except:
                            pass
                        
                    else:
                        upload_success = await self.uploader.upload_with_progress(
                            final_video, user_id, drama_title, episode_num,
                            lambda msg: self.uploader.update_message(user_id, status_msg.message_id, msg)
                        )
                        
                        if not upload_success:
                            failed += 1
                            continue
                    
                    successful += 1
                    
                except Exception as e:
                    logger.error(f"Upload failed for episode {episode_num}: {e}")
                    failed += 1
                    continue
                
                # Cleanup episode files immediately after upload
                if DELETE_AFTER_UPLOAD:
                    await FileCleanup.cleanup_episode_files(
                        video_path=video_path if video_path.exists() else None,
                        subtitle_path=subtitle_path if subtitle_path.exists() else None,
                        output_path=output_path if output_path.exists() else None,
                        json_path=None,
                        delay=2
                    )
                
                if is_batch and idx < len(selected_episodes):
                    await asyncio.sleep(3)
            
            # Final status
            if is_batch:
                final_text = (
                    f"✅ <b>Batch Download Selesai!</b>\n\n"
                    f"Berhasil: {successful} episode\n"
                    f"Gagal: {failed} episode\n\n"
                    f"<i>Semua file telah dibersihkan</i>"
                )
                try:
                    await self.uploader.update_message(
                        user_id,
                        status_msg.message_id,
                        final_text
                    )
                except:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=final_text,
                        parse_mode="HTML"
                    )
            
        except Exception as e:
            logger.error(f"Error in batch processing: {e}")
            try:
                await self.uploader.update_message(
                    user_id,
                    status_msg.message_id,
                    f"❌ <b>Error:</b> {str(e)}\n\n<i>Membersihkan file...</i>"
                )
            except:
                pass
        
        finally:
            # Bersihkan semua file dan session
            await self._cleanup_user_session(user_id, context, "proses selesai")
            
            # Cleanup old files (more than 24 hours)
            asyncio.create_task(FileCleanup.cleanup_old_files(DOWNLOAD_DIR, minutes=5))
        
        return ConversationHandler.END
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors and cleanup files & session"""
        import httpx
        from telegram.error import TimedOut, NetworkError, BadRequest
        
        # Abaikan error timeout/network agar tidak hapus session user
        if isinstance(context.error, (TimedOut, NetworkError, httpx.ReadTimeout, httpx.ConnectTimeout)):
            logger.warning(f"Ignored timeout/network error: {context.error}")
            return
            
        if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
            return
            
        logger.error(f"Update {update} caused error {context.error}")
        
        # Try to cleanup user files and session
        if update and update.effective_user:
            user_id = update.effective_user.id
            await self._cleanup_user_session(user_id, context, f"error handler: {context.error}")
        
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ Terjadi kesalahan. File dan session dibersihkan. Silakan coba lagi.\n"
                    "*(Semua file yang diunduh akan otomatis terhapus dalam 5 menit)*"
                )
            except:
                pass

def main():
    """Main entry point"""
    bot = DownloaderBot()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Document.FileExtension("json"), bot.handle_json_file)],
        states={
            AWAITING_BATCH_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_batch_choice)],
            AWAITING_CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.process_video)],
        },
        fallbacks=[CommandHandler("cancel", bot.cancel)],
        name="download_conversation",
        persistent=False
    )
    
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("help", bot.help))
    app.add_handler(CommandHandler("cancel", bot.cancel))
    app.add_handler(conv_handler)
    app.add_error_handler(bot.error_handler)
    
    print("=" * 60)
    print("🚀 Bot Downloader Telegram - Batch Download + Subtitle ID")
    print("=" * 60)
    print("Fitur:")
    print("✅ Auto detect semua episode (termasuk dramabox v2)")
    print("✅ Auto detect subtitle Indonesia (id, ind, bahasa, dll)")
    print("✅ Hard subtitle otomatis")
    print("✅ Batch download")
    print("✅ Auto cleanup files setelah selesai")
    print("✅ Auto hapus session jika error")
    print("-" * 60)
    print(f"Bot Token: {BOT_TOKEN[:10]}...{BOT_TOKEN[-5:]}")
    print(f"Download Directory: {DOWNLOAD_DIR.absolute()}")
    print(f"Auto Cleanup: {'ON' if DELETE_AFTER_UPLOAD else 'OFF'}")
    print(f"Cleanup Delay: {CLEANUP_DELAY} seconds")
    print(f"Max Concurrent Downloads: {MAX_CONCURRENT_DOWNLOADS}")
    print(f"Session Timeout: {SESSION_TIMEOUT} seconds")
    print("=" * 60)
    print("Bot started! Press Ctrl+C to stop.")
    print("📱 Waiting for JSON files...")
    print("=" * 60)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()