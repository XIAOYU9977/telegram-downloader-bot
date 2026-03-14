import asyncio
import asyncio.subprocess
import uuid
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Tuple, Optional, List, Literal, cast
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
import aiofiles
import aiohttp

from config import (
    BOT_TOKEN, DOWNLOAD_DIR, ALLOWED_USERS, DELETE_AFTER_UPLOAD,
    MAX_CONCURRENT_DOWNLOADS, SESSION_TIMEOUT,
    DOWNLOAD_TIMEOUT, PROCESSING_TIMEOUT, UPLOAD_TIMEOUT, MAX_RETRIES,
    CLEANUP_DELAY, CLEANUP_ON_ERROR, TARGET_FILE_SIZE_MB
)
from downloader import DownloadManager
from processor import VideoProcessor
from uploader import TelegramUploader
from session import SessionManager
from utils import (
    JSONParser, SubtitleDetector, FileCleanup,
    cleanup_file, format_size, logger
)
try:
    from vigloo import ViglooParser
except ImportError:
    ViglooParser = None
from task_tracker import TaskTracker

# Conversation states
AWAITING_CONFIRMATION        = 0
AWAITING_BATCH_CHOICE        = 1
AWAITING_SUBTITLE_CHOICE     = 2   # /l — tanya subtitle setelah deteksi
AWAITING_BATCH_SUBTITLE      = 3   # /batch — tanya subtitle sekali di awal
AWAITING_DRAMAWAVE_SUBTITLE  = 4   # dramawave JSON — pilih subtitle mode
AWAITING_SOFTSUB_CHOICE      = 5   # JSON dengan subtitle terpisah — pilih softsub/hardsub/terpisah/none
AWAITING_FORMAT_CHOICE       = 6   # Memilih format MKV atau MP4

class DownloaderBot:
    def __init__(self):
        self.task_tracker = TaskTracker()
        self.download_manager = DownloadManager(self.task_tracker)
        self.video_processor = VideoProcessor(self.task_tracker)
        from telegram import Bot
        self.uploader = TelegramUploader(Bot(BOT_TOKEN))
        self.session_manager = SessionManager()
        self.cleanup = FileCleanup()
        self.download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        keyboard = [
            [
                InlineKeyboardButton("🎥 1080p", callback_data="set_res_1080p"),
                InlineKeyboardButton("🎥 720p", callback_data="set_res_720p")
            ],
            [
                InlineKeyboardButton("🎥 480p", callback_data="set_res_480p"),
                InlineKeyboardButton("🎥 360p", callback_data="set_res_360p")
            ],
            [
                InlineKeyboardButton("🎞 Format: MP4", callback_data="set_fmt_mp4"),
                InlineKeyboardButton("🎞 Format: MKV", callback_data="set_fmt_mkv")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Cek pengaturan saat ini
        current_res = context.user_data.get("default_resolution", "1080p")
        current_fmt = context.user_data.get("default_format", "mp4")

        await update.message.reply_text(
            f"🎬 <b>Video Downloader Bot</b>\n\n"
            f"Kirimkan saya file <b>.json</b> untuk mulai mendownload video.\n\n"
            f"⚙️ <b>Pengaturan Saat Ini:</b>\n"
            f"Kualitas: <b>{current_res}</b>\n"
            f"Format Output: <b>{current_fmt.upper()}</b>\n\n"
            f"Fitur:\n"
            f"✅ Deteksi semua episode dari JSON\n"
            f"✅ Pilih download 1 episode atau semua episode\n"
            f"✅ Auto detect subtitle Indonesia\n"
            f"✅ Download langsung dari HLS stream / .m3u8\n"
            f"✅ Mendukung format JSON: dramabox, dramawave, dll\n"
            f"✅ Batch download multi-episode sekaligus\n\n"
            f"Download langsung:\n"
            f"  <code>/l [judul] [link]</code>\n\n"
            f"Batch download:\n"
            f"  <code>/batch [judul] [link1] [link2] ...</code>\n\n"
            f"👇 <b>Ubah pengaturan default menggunakan tombol di bawah:</b>",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )
    
    async def handle_settings_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Menangani pilihan pengaturan dari tombol di menu /start."""
        query = update.callback_query
        await query.answer()

        data = query.data
        if data.startswith("set_res_"):
            resolution = data.replace("set_res_", "")
            context.user_data["default_resolution"] = resolution
            await query.answer(f"✅ Resolusi default diubah menjadi {resolution}", show_alert=True)
            
        elif data.startswith("set_fmt_"):
            fmt = data.replace("set_fmt_", "")
            context.user_data["default_format"] = fmt
            await query.answer(f"✅ Format default diubah menjadi {fmt.upper()}", show_alert=True)
            
        # Update tampilan menu Settings
        current_res = context.user_data.get("default_resolution", "1080p")
        current_fmt = context.user_data.get("default_format", "mp4")

        keyboard = [
            [
                InlineKeyboardButton(f"{'✅ ' if current_res == '1080p' else ''}🎥 1080p", callback_data="set_res_1080p"),
                InlineKeyboardButton(f"{'✅ ' if current_res == '720p' else ''}🎥 720p", callback_data="set_res_720p")
            ],
            [
                InlineKeyboardButton(f"{'✅ ' if current_res == '480p' else ''}🎥 480p", callback_data="set_res_480p"),
                InlineKeyboardButton(f"{'✅ ' if current_res == '360p' else ''}🎥 360p", callback_data="set_res_360p")
            ],
            [
                InlineKeyboardButton(f"{'✅ ' if current_fmt == 'mp4' else ''}🎞 Format: MP4", callback_data="set_fmt_mp4"),
                InlineKeyboardButton(f"{'✅ ' if current_fmt == 'mkv' else ''}🎞 Format: MKV", callback_data="set_fmt_mkv")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Update text pesan
        text = (
            f"🎬 <b>Video Downloader Bot</b>\n\n"
            f"Kirimkan saya file <b>.json</b> untuk mulai mendownload video.\n\n"
            f"⚙️ <b>Pengaturan Saat Ini:</b>\n"
            f"Kualitas: <b>{current_res}</b>\n"
            f"Format Output: <b>{current_fmt.upper()}</b>\n\n"
            f"Fitur:\n"
            f"✅ Deteksi semua episode dari JSON\n"
            f"✅ Pilih download 1 episode atau semua episode\n"
            f"✅ Auto detect subtitle Indonesia\n"
            f"✅ Download langsung dari HLS stream / .m3u8\n"
            f"✅ Mendukung format JSON: dramabox, dramawave, dll\n"
            f"✅ Batch download multi-episode sekaligus\n\n"
            f"Download langsung:\n"
            f"  <code>/l [judul] [link]</code>\n\n"
            f"Batch download:\n"
            f"  <code>/batch [judul] [link1] [link2] ...</code>\n\n"
            f"👇 <b>Ubah pengaturan default menggunakan tombol di bawah:</b>"
        )
        
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            pass

    # =========================================================================
    # Helper: build confirmation inline keyboard (format + quality + OK/BATAL)
    # =========================================================================
    def _build_confirmation_keyboard(self, selected_fmt: str = "mp4", selected_res: str = "1080p"):
        """
        Build inline keyboard for download confirmation.
        Shows format (MP4/MKV) and quality (1080p/720p/480p/360p) as toggleable buttons,
        plus ✅ Mulai Download and ❌ Batal buttons.
        """
        fmt_mp4 = "✅ MP4" if selected_fmt == "mp4" else "MP4"
        fmt_mkv = "✅ MKV" if selected_fmt == "mkv" else "MKV"

        res_1080 = "✅ 1080p" if selected_res == "1080p" else "1080p"
        res_720  = "✅ 720p"  if selected_res == "720p"  else "720p"
        res_480  = "✅ 480p"  if selected_res == "480p"  else "480p"
        res_360  = "✅ 360p"  if selected_res == "360p"  else "360p"

        keyboard = [
            # Format row
            [
                InlineKeyboardButton(f"🎞 {fmt_mp4}", callback_data="conf_fmt_mp4"),
                InlineKeyboardButton(f"🎞 {fmt_mkv}", callback_data="conf_fmt_mkv"),
            ],
            # Quality row
            [
                InlineKeyboardButton(f"🎥 {res_1080}", callback_data="conf_res_1080p"),
                InlineKeyboardButton(f"🎥 {res_720}",  callback_data="conf_res_720p"),
            ],
            [
                InlineKeyboardButton(f"🎥 {res_480}",  callback_data="conf_res_480p"),
                InlineKeyboardButton(f"🎥 {res_360}",  callback_data="conf_res_360p"),
            ],
            # Action row
            [
                InlineKeyboardButton("✅ Mulai Download", callback_data="conf_ok"),
                InlineKeyboardButton("❌ Batal",          callback_data="conf_cancel"),
            ],
        ]
        return InlineKeyboardMarkup(keyboard)

    async def handle_confirmation_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Callback handler for conf_fmt_*, conf_res_*, conf_ok, conf_cancel buttons
        on the confirmation message.
        """
        query = update.callback_query
        await query.answer()
        data = query.data
        user_id = update.effective_user.id

        # ── Toggle format ─────────────────────────────────────────────────
        if data.startswith("conf_fmt_"):
            fmt = data.replace("conf_fmt_", "")
            context.user_data["conf_format"] = fmt
            context.user_data["default_format"] = fmt
            # Rebuild keyboard and refresh message
            sel_fmt = context.user_data.get("conf_format", "mp4")
            sel_res = context.user_data.get("conf_resolution", "1080p")
            markup = self._build_confirmation_keyboard(sel_fmt, sel_res)
            try:
                await query.edit_message_reply_markup(reply_markup=markup)
            except Exception:
                pass
            return

        # ── Toggle quality ────────────────────────────────────────────────
        if data.startswith("conf_res_"):
            res = data.replace("conf_res_", "")
            context.user_data["conf_resolution"] = res
            context.user_data["default_resolution"] = res
            sel_fmt = context.user_data.get("conf_format", "mp4")
            sel_res = context.user_data.get("conf_resolution", "1080p")
            markup = self._build_confirmation_keyboard(sel_fmt, sel_res)
            try:
                await query.edit_message_reply_markup(reply_markup=markup)
            except Exception:
                pass
            return

        # ── Batal ─────────────────────────────────────────────────────────
        if data == "conf_cancel":
            await self._cleanup_user_session(user_id, context, "dibatalkan user via tombol")
            try:
                await query.edit_message_text("✅ Proses dibatalkan.")
            except Exception:
                pass
            return

        # ── OK / Mulai Download ───────────────────────────────────────────
        if data == "conf_ok":
            # Ambil pilihan user
            sel_fmt = context.user_data.get("conf_format", "mp4")
            sel_res = context.user_data.get("conf_resolution", "1080p")

            # Update global default juga
            context.user_data["default_format"] = sel_fmt
            context.user_data["default_resolution"] = sel_res

            # Hapus tombol dari pesan konfirmasi
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            # ── Cek apakah ini alur /l atau /batch ────────────────────────
            pending_single = context.user_data.get("pending_single_conf")
            pending_batch_link = context.user_data.get("pending_batch_link_conf")

            if pending_single:
                # /l single link flow
                context.user_data.pop("pending_single_conf", None)
                p = pending_single

                # Rebuild paths with correct format extension
                job_uuid = uuid.uuid4().hex[:6]
                user_title = p["title"]
                filename = f"{user_title}.{sel_fmt}"
                
                # Gunakan user-specific directory
                user_dir = DOWNLOAD_DIR / str(user_id)
                user_dir.mkdir(parents=True, exist_ok=True)
                
                video_path  = user_dir / f"{user_title}_{job_uuid}.{sel_fmt}"
                output_path = user_dir / f"{user_title}_{job_uuid}_out.{sel_fmt}"

                if not self.uploader:
                    self.uploader = TelegramUploader(context.bot)

                status_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📥 <b>Download dimulai</b>\n"
                         f"🎬 <b>Judul:</b> {user_title}\n"
                         f"📦 <b>Format:</b> {sel_fmt.upper()} | <b>Kualitas:</b> {sel_res}\n"
                         f"⏳ Menyiapkan download...",
                    parse_mode="HTML"
                )

                self.session_manager.create_session(
                    user_id,
                    {"source": "direct_link", "url": p["url"], "title": user_title},
                    json_file_path=None
                )
                self.session_manager.set_progress_message(user_id, status_msg.message_id)

                asyncio.create_task(
                    self._run_single_download(
                        user_id=user_id,
                        raw_url=p["url"],
                        video_path=video_path,
                        output_path=output_path,
                        status_msg=status_msg,
                        display_title=user_title,
                        filename=filename,
                        subtitle_url=p.get("subtitle_url", ""),
                        subtitle_mode=p.get("subtitle_mode", "none"),
                        cleanup_session=True,
                        output_format=sel_fmt,
                        target_resolution=sel_res,
                    )
                )
                return

            if pending_batch_link:
                # /batch link flow
                context.user_data.pop("pending_batch_link_conf", None)
                p = pending_batch_link

                if not self.uploader:
                    self.uploader = TelegramUploader(context.bot)

                status_msg = await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📥 <b>Batch download dimulai</b>\n"
                         f"🎬 <b>Series:</b> {p['series_title']}\n"
                         f"📦 <b>Total:</b> {len(p['urls'])} episode\n"
                         f"📦 <b>Format:</b> {sel_fmt.upper()} | <b>Kualitas:</b> {sel_res}\n"
                         f"⏳ Memulai proses...",
                    parse_mode="HTML"
                )

                self.session_manager.create_session(
                    user_id,
                    {"source": "batch", "title": p["series_title"], "total": len(p["urls"])},
                    json_file_path=None
                )
                self.session_manager.set_progress_message(user_id, status_msg.message_id)

                asyncio.create_task(
                    self._run_batch_download(
                        user_id=user_id,
                        urls=p["urls"],
                        series_title=p["series_title"],
                        status_msg=status_msg,
                        subtitle_mode=p.get("subtitle_mode", "none"),
                        output_format=sel_fmt,
                        target_resolution=sel_res,
                    )
                )
                return

            # ── JSON flow confirmation ────────────────────────────────────
            selected_episodes = context.user_data.get('selected_episodes', [])
            if not selected_episodes:
                episodes = context.user_data.get('episodes', [])
                if episodes and len(episodes) == 1:
                    selected_episodes = episodes
                else:
                    try:
                        await query.edit_message_text("❌ Tidak ada episode yang dipilih.")
                    except Exception:
                        pass
                    await self._cleanup_user_session(user_id, context, "tidak ada episode dipilih")
                    return

            # Trigger process_video logic via callback
            drama_title = context.user_data.get('drama_title', 'Video')
            json_path = Path(context.user_data.get('json_path', ''))
            is_batch = len(selected_episodes) > 1

            status_msg = await context.bot.send_message(
                chat_id=user_id,
                text=f"📥 <b>{'Batch download' if is_batch else 'Download'} dimulai</b>\n"
                     f"🎬 <b>Judul:</b> {drama_title}\n"
                     f"📦 <b>Format:</b> {sel_fmt.upper()} | <b>Kualitas:</b> {sel_res}\n"
                     f"📦 <b>Total:</b> {len(selected_episodes)} episode\n"
                     f"⏳ Memproses...",
                parse_mode="HTML"
            )

            if not self.uploader:
                self.uploader = TelegramUploader(context.bot)

            self.session_manager.set_progress_message(user_id, status_msg.message_id)

            asyncio.create_task(
                self._process_episodes(
                    user_id=user_id,
                    selected_episodes=selected_episodes,
                    drama_title=drama_title,
                    json_path=json_path,
                    is_batch=is_batch,
                    status_msg=status_msg,
                    context=context,
                    output_format=sel_fmt,
                    target_resolution=sel_res,
                )
            )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        await update.message.reply_text(
            "📖 <b>Bantuan</b>\n\n"
            "<b>━━ Download Single: /l ━━</b>\n"
            "Format: <code>/l [judul] [link]</code>\n"
            "Format: <code>/l [link]</code>  (judul otomatis)\n\n"
            "Contoh:\n"
            "  <code>/l Drama_Ep1 https://new.rishort.com/api/goodshort/hls/13341728/31001069305/playlist.m3u8?q=1080p</code>\n\n"
            "• Mendukung link .m3u8 (HLS/Stream)\n"
            "• Mendukung Rishort, GoodShort, HLS Proxy\n"
            "• Jika subtitle terdeteksi → bot akan bertanya cara prosesnya\n"
            "• Nama file sesuai judul user: <b>Drama_Ep1.mp4</b>\n"
            "• File lokal otomatis dihapus setelah dikirim\n"
            "• Kompresi otomatis jika &gt; 50MB\n\n"
            "<b>━━ Batch Download: /batch ━━</b>\n"
            "Format: <code>/batch [judul] [link1] [link2] ...</code>\n"
            "<b>Maksimal 200 link per perintah</b>\n\n"
            "Contoh:\n"
            "  <code>/batch Drama https://new.rishort.com/.../31001069305/playlist.m3u8 https://new.rishort.com/.../31001069306/playlist.m3u8</code>\n\n"
            "• Nama file otomatis: <b>Drama_Ep01.mp4</b>, <b>Drama_Ep02.mp4</b>, ...\n"
            "• Jika subtitle ditemukan → bot bertanya sekali, berlaku ke semua episode\n"
            "• Proses berurutan sesuai urutan link\n"
            "• Jika episode gagal → notifikasi error, lanjut ke episode berikutnya\n"
            "• Laporan ringkasan dikirim di akhir\n"
            "• Jika &gt;200 link → bot menolak dan minta kirim batch berikutnya\n\n"
            "<b>━━ Pilihan Subtitle ━━</b>\n"
            "Jika subtitle terdeteksi, bot akan menawarkan:\n"
            "  1️⃣ Softsub (track embedded di video, bisa dimatikan)\n"
            "  2️⃣ Hardsub (subtitle dibakar ke dalam video)\n"
            "  3️⃣ Subtitle terpisah (file .srt dikirim sendiri)\n"
            "  4️⃣ Tanpa subtitle\n\n"
            "<i>Untuk /l dan /batch via HLS: pilihan 1=Gabung, 2=Tanpa, 3=Terpisah.</i>\n\n"
            "<b>━━ Format JSON yang didukung ━━</b>\n"
            "• dramabox (v1 & v2), dramawave, flikreels\n"
            "• goodshort, freereels, stardust, vigloo, meloshort, <b>velolo</b>\n\n"
            "Gunakan /cancel untuk membatalkan proses.",
            parse_mode="HTML"
        )
    
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel current operation and cleanup ALL files — works from any state"""
        user_id = update.effective_user.id
        
        # 1. Cancel active tasks & subprocesses
        await self.task_tracker.cancel_all(user_id)
        
        # 2. Cleanup session & files
        await self._cleanup_user_session(user_id, context, "dibatalkan oleh user (/cancel)")
        
        await update.message.reply_text(
            "✅ *Semua proses dibatalkan.*\n"
            "🧹 Session & file telah dibersihkan.",
            parse_mode="Markdown"
        )
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
            # ── Velolo format ──────────────────────────────────────────────────
            if "videoInfo" in data and "episodesInfo" in data:
                info = data["videoInfo"]
                title = info.get("name", "Video")
                rows = data["episodesInfo"].get("rows", [])
                if rows:
                    first_ep = rows[0]
                    episode = str(first_ep.get("orderNumber", 0) + 1)
                    if first_ep.get("zimu"):
                        has_subtitle = True
                return title, episode, has_subtitle

            # ── Shortmax format ───────────────────────────────────────────────
            if "shortPlayId" in data or "shortPlayName" in data:
                title = data.get("shortPlayName", "Shortmax Video")
                episodes = data.get("episodes", [])
                if episodes:
                    first_ep = episodes[0]
                    episode = str(first_ep.get("episodeNumber", "1"))
                return title, episode, False

            # ── Netshort format ───────────────────────────────────────────────
            if "shortPlayEpisodeInfos" in data:
                title = data.get("shortPlayName") or data.get("title") or "Netshort Video"
                episodes = data.get("shortPlayEpisodeInfos", [])
                if episodes:
                    first_ep = episodes[0]
                    episode = str(first_ep.get("episodeNo") or first_ep.get("episodeNumber", "1"))
                return title, episode, False

            # Dramaflickreels format
            if "drama" in data and "episodes" in data:
                drama_info = data["drama"]
                title = drama_info.get("title", "Drama")
                eps_list = data.get("episodes", [])
                if eps_list:
                    first = eps_list[0]
                    raw = first.get("raw", {})
                    episode = str(raw.get("chapter_num", first.get("index", 0) + 1))
                return title, episode, False

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
    
    def _extract_velolo_cover(self, data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """
        Ekstrak URL cover dan judul dari format velolo.
        Returns (cover_url, title) atau (None, None) jika bukan format velolo.
        """
        if "videoInfo" in data and "episodesInfo" in data:
            info = data["videoInfo"]
            cover_url = info.get("cover")
            title = info.get("name")
            return cover_url, title
        return None, None

    def _extract_velolo_episodes(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Ekstrak semua episode dari format velolo.
        Returns list episode dengan keys: episode, title, url, subtitle_url
        """
        episodes = []
        if "videoInfo" not in data or "episodesInfo" not in data:
            return episodes
        
        drama_title = data["videoInfo"].get("name", "Video")
        rows = data["episodesInfo"].get("rows", [])
        
        for row in rows:
            order = row.get("orderNumber", 0)
            ep_num = str(order + 1)
            video_url = row.get("videoAddress", "")
            subtitle_url = row.get("zimu", "") or ""
            
            if video_url:
                episodes.append({
                    "episode": ep_num,
                    "title": drama_title,
                    "url": video_url,
                    "subtitle_url": subtitle_url,
                    "subtitle_mode": "separate",   # velolo: selalu kirim sub terpisah
                    "source": "velolo",
                })
        
        return episodes

    # ── Dramawave helpers ─────────────────────────────────────────────────────

    def _is_dramawave(self, data: Dict[str, Any]) -> bool:
        """Deteksi apakah JSON adalah format dramawave."""
        try:
            return (
                "data" in data
                and isinstance(data["data"], dict)
                and "info" in data["data"]
                and "episode_list" in data["data"]["info"]
            )
        except Exception:
            return False

    def _extract_dramawave_info(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Ekstrak info dramawave: cover, name, desc, eps_count, eps_with_sub, episodes.
        Episode sudah termasuk subtitle_url (id-ID), subtitle_mode akan diset kemudian.
        """
        info = data["data"]["info"]
        name      = info.get("name", "Video")
        cover_url = info.get("cover", "")
        desc      = info.get("desc", "")
        ep_list   = info.get("episode_list", [])

        episodes = []
        for item in ep_list:
            ep_num    = str(item.get("index", len(episodes) + 1))
            video_url = (
                item.get("external_audio_h264_m3u8")
                or item.get("external_audio_h265_m3u8")
                or item.get("m3u8_url")
                or item.get("video_url")
                or ""
            )
            # Cari subtitle id-ID
            subtitle_url = None
            for sub in item.get("subtitle_list", []):
                lang = sub.get("language", "")
                if lang.lower().startswith("id"):
                    subtitle_url = sub.get("subtitle") or sub.get("url")
                    break

            if video_url:
                episodes.append({
                    "episode":      ep_num,
                    "title":        name,
                    "url":          video_url,
                    "subtitle_url": subtitle_url,
                    "source":       "dramawave",
                    # subtitle_mode akan diisi oleh handler pilihan user
                })

        eps_with_sub = sum(1 for ep in episodes if ep.get("subtitle_url"))
        return {
            "name":         name,
            "cover_url":    cover_url,
            "desc":         desc,
            "episodes":     episodes,
            "eps_with_sub": eps_with_sub,
        }

    async def handle_dramawave_subtitle_choice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """
        Handle pilihan subtitle dramawave (dan format lain dengan subtitle terpisah):
        1 → softsub (embedded track di MP4, tidak dibakar)
        2 → hardsub (subtitle dibakar ke video)
        3 → subtitle terpisah (.srt dikirim sendiri)
        4 → tanpa subtitle
        BATAL → batalkan
        """
        user_id = update.effective_user.id
        choice  = update.message.text.strip()

        if choice.upper() == "BATAL":
            await self._cleanup_user_session(user_id, context, "dibatalkan user")
            await update.message.reply_text("✅ Proses dibatalkan.")
            return ConversationHandler.END

        mode_map = {"1": "softsub", "2": "embed", "3": "separate", "4": "none"}
        subtitle_mode = mode_map.get(choice)

        if not subtitle_mode:
            await update.message.reply_text(
                "⚠️ Pilihan tidak valid.\n"
                "Ketik <b>1</b>, <b>2</b>, <b>3</b>, atau <b>4</b>.\n"
                "Ketik <b>BATAL</b> untuk membatalkan.",
                parse_mode="HTML"
            )
            return AWAITING_DRAMAWAVE_SUBTITLE

        # Terapkan subtitle_mode ke semua episode
        episodes = context.user_data.get("episodes", [])
        for ep in episodes:
            ep["subtitle_mode"] = subtitle_mode

        context.user_data["episodes"] = episodes

        mode_label = {
            "softsub":  "💬 Softsub (subtitle di dalam video, bisa dimatikan)",
            "embed":    "🔥 Hardsub (subtitle dibakar ke video)",
            "separate": "📄 Subtitle terpisah (.srt dikirim sendiri)",
            "none":     "🚫 Tanpa subtitle",
        }[subtitle_mode]

        drama_title   = context.user_data.get("drama_title", "Video")
        total         = len(episodes)
        eps_with_sub  = sum(1 for ep in episodes if ep.get("subtitle_url"))

        episode_list_text = "\n".join([
            f"• Episode {ep['episode']}: {'[SUB] ' if ep.get('subtitle_url') else ''}{ep['title'][:30]}"
            for ep in episodes[:10]
        ])
        if total > 10:
            episode_list_text += f"\n• ... dan {total - 10} episode lainnya"

        choice_text = (
            f"📋 <b>Ditemukan {total} Episode</b>\n\n"
            f"Judul: {drama_title}\n"
            f"Subtitle Indonesia: {eps_with_sub} episode\n"
            f"Mode subtitle: {mode_label}\n\n"
            f"Daftar Episode:\n{episode_list_text}\n\n"
            "📝 <b>Pilihan Download:</b>\n\n"
            "Ketik nomor episode (contoh: 1, 1-5, 1,3,5)\n"
            "Atau ketik <b>SEMUA</b> untuk download semua episode\n"
            "Ketik <b>BATAL</b> untuk membatalkan\n\n"
            "<i>File akan otomatis dihapus setelah selesai</i>"
        )
        await update.message.reply_text(choice_text, parse_mode="HTML")
        return AWAITING_BATCH_CHOICE

    async def handle_softsub_choice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """
        Handler pilihan subtitle untuk format JSON umum (non-dramawave/velolo)
        yang memiliki subtitle_url terpisah.
        1 → softsub, 2 → hardsub, 3 → terpisah, 4 → none
        Setelah memilih, lanjut ke AWAITING_BATCH_CHOICE atau AWAITING_CONFIRMATION.
        """
        user_id = update.effective_user.id
        choice  = update.message.text.strip()

        if choice.upper() == "BATAL":
            await self._cleanup_user_session(user_id, context, "dibatalkan user")
            await update.message.reply_text("✅ Proses dibatalkan.")
            return ConversationHandler.END

        mode_map = {"1": "softsub", "2": "embed", "3": "separate", "4": "none"}
        subtitle_mode = mode_map.get(choice)

        if not subtitle_mode:
            await update.message.reply_text(
                "⚠️ Pilihan tidak valid.\n"
                "Ketik <b>1</b>, <b>2</b>, <b>3</b>, atau <b>4</b>.\n"
                "Ketik <b>BATAL</b> untuk membatalkan.",
                parse_mode="HTML"
            )
            return AWAITING_SOFTSUB_CHOICE

        episodes = context.user_data.get('episodes', [])
        for ep in episodes:
            ep['subtitle_mode'] = subtitle_mode
        context.user_data['episodes'] = episodes

        mode_label = {
            "softsub":  "💬 Softsub",
            "embed":    "🔥 Hardsub",
            "separate": "📄 Subtitle terpisah",
            "none":     "🚫 Tanpa subtitle",
        }[subtitle_mode]

        drama_title = context.user_data.get('drama_title', 'Video')
        total = len(episodes)

        if total == 1:
            # Single episode → konfirmasi langsung
            ep = episodes[0]
            confirmation_text = (
                "📋 <b>Konfirmasi Data</b>\n\n"
                f"Judul: {drama_title}\n"
                f"Episode: {ep['episode']}\n"
                f"Mode Subtitle: {mode_label}\n\n"
                "Ketik:\n"
                "<b>OK</b> → untuk mulai proses\n"
                "<b>BATAL</b> → untuk membatalkan\n\n"
                "<i>File akan otomatis dihapus setelah selesai</i>"
            )
            await update.message.reply_text(confirmation_text, parse_mode="HTML")
            return AWAITING_CONFIRMATION
        else:
            # Multi-episode → tampilkan daftar
            episode_list = "\n".join([
                f"• Episode {ep['episode']}: {'[SUB] ' if ep.get('subtitle_url') else ''}{ep['title'][:30]}"
                for ep in episodes[:10]
            ])
            if total > 10:
                episode_list += f"\n• ... dan {total - 10} episode lainnya"

            eps_with_sub = sum(1 for ep in episodes if ep.get('subtitle_url'))
            choice_text = (
                f"📋 <b>Ditemukan {total} Episode</b>\n\n"
                f"Judul: {drama_title}\n"
                f"Subtitle Indonesia: {eps_with_sub} episode\n"
                f"Mode Subtitle: {mode_label}\n\n"
                f"Daftar Episode:\n{episode_list}\n\n"
                "📝 <b>Pilihan Download:</b>\n\n"
                "Ketik nomor episode (contoh: 1, 1-5, 1,3,5)\n"
                "Atau ketik <b>SEMUA</b> untuk download semua episode\n"
                "Ketik <b>BATAL</b> untuk membatalkan\n\n"
                "<i>File akan otomatis dihapus setelah selesai</i>"
            )
            await update.message.reply_text(choice_text, parse_mode="HTML")
            return AWAITING_BATCH_CHOICE

    async def handle_json_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle JSON file upload - extract all episodes and ask for choice"""
        user_id = update.effective_user.id
        
        if ALLOWED_USERS and user_id not in ALLOWED_USERS:
            await update.message.reply_text("⛔ Anda tidak diizinkan menggunakan bot ini.")
            return ConversationHandler.END
            
        if not self.uploader:
            self.uploader = TelegramUploader(context.bot)
        
        # Cek session aktif
        if self.session_manager.has_active_session(user_id):
            await update.message.reply_text(
                "⚠️ Masih ada proses yang berjalan. Harap tunggu hingga selesai.\n"
                "Ketik /cancel untuk membatalkan proses sebelumnya."
            )
            return ConversationHandler.END
        
        json_path = None
        url_fetched_path = context.user_data.pop('url_fetched_path', None)
        
        try:
            if url_fetched_path:
                json_path = Path(url_fetched_path)
            else:
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

            # ── Gunakan Universal Parser yang telah ditingkatkan ──────────────────
            universal_data = JSONParser.universal_parse(data)
            all_episodes = universal_data.get("all_episodes", [])
            drama_title = universal_data.get("title", "Video")
            cover_url = universal_data.get("cover_url")
            source = universal_data.get("source", "unknown")

            # ── Special processing: Vigloo async URL filling ──────────────────
            if source == "vigloo" and all_episodes and ViglooParser:
                status_filling = await update.message.reply_text("🔍 Fetching episode details from Vigloo...")
                try:
                    vigloo_parser = ViglooParser()
                    all_episodes = await vigloo_parser.fill_urls(all_episodes)
                    await status_filling.delete()
                except Exception as e:
                    logger.error(f"Vigloo URL filling failed: {e}")
                    await status_filling.edit_text("⚠️ Gagal mengambil detail dari Vigloo.")

            if cover_url:
                try:
                    await update.message.reply_photo(
                        photo=cover_url,
                        caption=f"🎬 <b>{drama_title}</b>",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            if not all_episodes:
                # Fallback ke video_url tunggal jika tidak ada array episode
                video_url = universal_data.get("url")
                subtitle_url = universal_data.get("subtitle_url")
                
                if not video_url:
                    await update.message.reply_text("❌ Tidak dapat menemukan episode atau URL video dalam JSON ini.")
                    await self._cleanup_user_session(user_id, context, "tidak ada episode")
                    return ConversationHandler.END

                title, episode, has_subtitle = self.extract_title_episode(data, filename)
                all_episodes = [{
                    "episode": episode,
                    "title": title,
                    "url": video_url,
                    "subtitle_url": subtitle_url
                }]
                drama_title = title

            context.user_data['episodes'] = all_episodes
            context.user_data['total_episodes'] = len(all_episodes)
            context.user_data['drama_title'] = drama_title
            context.user_data['json_path'] = str(json_path)

            eps_with_sub = sum(1 for ep in all_episodes if ep.get('subtitle_url'))
            
            # Tanya mode subtitle jika ada subtitle Indonesia
            if eps_with_sub > 0:
                await update.message.reply_text(
                    f"📝 <b>Subtitle Indonesia terdeteksi</b> ({eps_with_sub} episode)\n\n"
                    "Pilih cara memproses subtitle:\n\n"
                    "  <b>1</b> → 💬 Softsub (embedded track, bisa dimatikan)\n"
                    "  <b>2</b> → 🔥 Hardsub (subtitle dibakar ke video)\n"
                    "  <b>3</b> → 📄 Subtitle terpisah (.srt dikirim sendiri)\n"
                    "  <b>4</b> → 🚫 Tanpa subtitle\n\n"
                    "Ketik <b>BATAL</b> untuk membatalkan.",
                    parse_mode="HTML"
                )
                return AWAITING_SOFTSUB_CHOICE if len(all_episodes) == 1 else AWAITING_DRAMAWAVE_SUBTITLE

            # Tidak ada subtitle, langsung ke pemilihan episode
            for ep in all_episodes:
                ep["subtitle_mode"] = "none"
            
            if len(all_episodes) == 1:
                # Langsung konfirmasi jika cuma 1 episode
                ep = all_episodes[0]
                confirmation_text = (
                    "📋 <b>Konfirmasi Data</b>\n\n"
                    f"Judul: {drama_title}\n"
                    f"Episode: {ep['episode']}\n"
                    f"Subtitle Indonesia: Tidak Ada\n\n"
                    "Ketik:\n"
                    "<b>OK</b> → untuk mulai proses\n"
                    "<b>BATAL</b> → untuk membatalkan\n\n"
                    "<i>File akan otomatis dihapus setelah selesai</i>"
                )
                await update.message.reply_text(confirmation_text, parse_mode="HTML")
                return AWAITING_CONFIRMATION
            else:
                # Multi-episode → tampilkan daftar
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

                
                # Tanya mode subtitle jika ada episode dengan subtitle
                if eps_with_sub > 0:
                    await update.message.reply_text(
                        f"📝 <b>Subtitle Indonesia terdeteksi</b> ({eps_with_sub} dari {len(all_episodes)} episode)\n\n"
                        "Pilih cara memproses subtitle:\n\n"
                        "  <b>1</b> → 💬 Softsub (embedded track, bisa dimatikan)\n"
                        "  <b>2</b> → 🔥 Hardsub (subtitle dibakar ke video)\n"
                        "  <b>3</b> → 📄 Subtitle terpisah (.srt dikirim sendiri)\n"
                        "  <b>4</b> → 🚫 Tanpa subtitle\n\n"
                        "Ketik <b>BATAL</b> untuk membatalkan.",
                        parse_mode="HTML"
                    )
                    return AWAITING_SOFTSUB_CHOICE
                
                # Tidak ada subtitle — langsung tampilkan daftar episode
                episode_list = "\n".join([
                    f"• Episode {ep['episode']}: {ep['title'][:30]}"
                    for ep in all_episodes[:10]
                ])
                if len(all_episodes) > 10:
                    episode_list += f"\n• ... dan {len(all_episodes) - 10} episode lainnya"
                
                choice_text = (
                    f"📋 <b>Ditemukan {len(all_episodes)} Episode</b>\n\n"
                    f"Judul: {drama_title}\n\n"
                    f"Daftar Episode:\n{episode_list}\n\n"
                    "📝 <b>Pilihan Download:</b>\n\n"
                    "Ketik nomor episode (contoh: 1, 1-5, 1,3,5)\n"
                    "Atau ketik <b>SEMUA</b> untuk download semua episode\n"
                    "Ketik <b>BATAL</b> untuk membatalkan\n\n"
                    "<i>File akan otomatis dihapus setelah selesai</i>"
                )
                await update.message.reply_text(choice_text, parse_mode="HTML")
                return AWAITING_BATCH_CHOICE
            
            return ConversationHandler.END
            
        except Exception as e:
            logger.error(f"Error handling JSON file: {e}")
            await self._cleanup_user_session(user_id, context, f"error: {str(e)}")
            await update.message.reply_text(f"❌ Error: {str(e)}")
            return ConversationHandler.END

    async def handle_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle JSON URL directly"""
        user_id = update.effective_user.id
        url = update.message.text.strip()
        
        if not url.startswith(('http://', 'https://')):
            return
            
        if self.session_manager.has_active_session(user_id):
            await update.message.reply_text("⚠️ Masih ada proses berjalan.")
            return

        status_msg = await update.message.reply_text("🌐 <b>Menganalisa URL...</b>", parse_mode="HTML")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        await status_msg.edit_text(f"❌ Gagal fetch URL: HTTP {resp.status}")
                        return
                    
                    try:
                        data = await resp.json()
                    except:
                        # Mungkin ini .json file link tapi response type bukan json
                        content = await resp.text()
                        import json
                        data = json.loads(content)
            
            # Simpan ke temp file agar kompatibel dengan handle_json_file
            json_path = DOWNLOAD_DIR / f"{user_id}_{uuid.uuid4()}.json"
            async with aiofiles.open(json_path, 'w', encoding='utf-8') as f:
                import json
                await f.write(json.dumps(data))
            
            # Bungkus sebagai document mock untuk reusable logic
            class MockDoc:
                def __init__(self, path, name):
                    self.file_name = name
                    self.path = path
                async def get_file(self):
                    class MockFile:
                        def __init__(self, p): self.p = p
                        async def download_to_drive(self, target): pass # sudah ada
                    return MockFile(self.path)

            update.message.document = MockDoc(json_path, "api_response.json")
            # Jalankan handle_json_file (perlu penyesuaian sedikit agar tidak re-download)
            # Tapi cara termudah adalah panggil logic-nya langsung atau copy-paste
            # Mari kita panggil handle_json_file tapi bypass download part.
            
            # Re-implementing handle_json_file logic here for simplicity & custom URL source
            self.session_manager.create_session(user_id, data, str(json_path))
            
            # Use universal parser to detect if it's a streaming JSON
            results = JSONParser.universal_parse(data)
            if results['videos']:
                logger.info(f"Universal parser found {len(results['videos'])} videos and {len(results['subtitles'])} subtitles")
                # Jika universal parser menemukan sesuatu, kita prioritaskan itu
                # (Logic ini bisa diperluas untuk otomatisasi lebih lanjut)
            
            # Continue with existing logic
            await status_msg.delete()
            return await self.handle_json_file(update, context, api_data=data, api_path=json_path)
            
        except Exception as e:
            logger.error(f"URL handle error: {e}")
            await status_msg.edit_text(f"❌ Error: {str(e)}")

    async def handle_callback_mi(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle MEDIA INFO button click"""
        query = update.callback_query
        await query.answer()
        
        _, filename = query.data.split("|")
        video_path = DOWNLOAD_DIR / filename
        
        if not video_path.exists():
            await query.answer("❌ File sudah dihapus dari server.", show_alert=True)
            return
            
        info = await self.video_processor.get_detailed_mediainfo_string(video_path)
        
        keyboard = [[InlineKeyboardButton("❌ Tutup", callback_data="close_mi")]]
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=info,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_close_mi(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.callback_query.message.delete()
    
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

            # Init confirmation selections from user defaults
            sel_fmt = context.user_data.get("default_format", "mp4")
            sel_res = context.user_data.get("default_resolution", "1080p")
            context.user_data["conf_format"] = sel_fmt
            context.user_data["conf_resolution"] = sel_res

            confirmation_text = (
                "📋 <b>Konfirmasi Batch Download</b>\n\n"
                f"🎬 <b>Judul:</b> {context.user_data.get('drama_title', 'Video')}\n"
                f"📦 <b>Total Episode:</b> {len(selected_episodes)}\n"
                f"💬 <b>Subtitle Indonesia:</b> {eps_with_sub} episode\n\n"
                "👇 <b>Pilih format dan kualitas, lalu tekan Mulai Download:</b>\n\n"
                "<i>File akan otomatis dihapus setelah selesai</i>"
            )

            markup = self._build_confirmation_keyboard(sel_fmt, sel_res)
            await update.message.reply_text(confirmation_text, parse_mode="HTML", reply_markup=markup)
            return ConversationHandler.END
        
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

            # Init confirmation selections from user defaults
            sel_fmt = context.user_data.get("default_format", "mp4")
            sel_res = context.user_data.get("default_resolution", "1080p")
            context.user_data["conf_format"] = sel_fmt
            context.user_data["conf_resolution"] = sel_res

            confirmation_text = (
                "📋 <b>Konfirmasi Download</b>\n\n"
                f"🎬 <b>Judul:</b> {context.user_data.get('drama_title', 'Video')}\n"
                f"📺 <b>Episode:</b> {episode_list}\n"
                f"📦 <b>Total:</b> {len(selected_episodes)} episode\n"
                f"💬 <b>Subtitle Indonesia:</b> {eps_with_sub} episode\n\n"
                "👇 <b>Pilih format dan kualitas, lalu tekan Mulai Download:</b>\n\n"
                "<i>File akan otomatis dihapus setelah selesai</i>"
            )

            markup = self._build_confirmation_keyboard(sel_fmt, sel_res)
            await update.message.reply_text(confirmation_text, parse_mode="HTML", reply_markup=markup)
            return ConversationHandler.END
            
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
        """Handle confirmation and start processing with global concurrency limit"""
        user_id = update.effective_user.id
        
        # Concurrency Limiter: Wait for slot if 2 users already downloading
        async with self.download_semaphore:
            logger.info(f"🟢 User {user_id} acquired download slot")
            return await self._process_video_locked(update, context)

    async def _process_video_locked(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Internal processing after semaphore acquisition"""
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
            
        self.session_manager.set_progress_message(user_id, status_msg.message_id)

        # Ambil pilihan user
        sel_fmt = context.user_data.get("conf_format", context.user_data.get("default_format", "mp4"))
        sel_res = context.user_data.get("conf_resolution", context.user_data.get("default_resolution", "1080p"))

        asyncio.create_task(
            self._process_episodes(
                user_id=user_id,
                selected_episodes=selected_episodes,
                drama_title=drama_title,
                json_path=json_path,
                is_batch=is_batch,
                status_msg=status_msg,
                context=context,
                output_format=sel_fmt,
                target_resolution=sel_res,
            )
        )
        return ConversationHandler.END
    

    # =========================================================================
    # CONSTANTS
    # =========================================================================
    MAX_BATCH_EPISODES = 200

    # =========================================================================
    # HELPERS
    # =========================================================================
    @staticmethod
    def _sanitize_filename(raw: str, max_len: int = 60) -> str:
        """Ubah judul menjadi nama file aman (tanpa karakter terlarang)."""
        safe = re.sub(r'[\\/:*?"<>|]', '', raw)
        safe = re.sub(r'\s+', '_', safe.strip())
        safe = re.sub(r'_+', '_', safe)
        return safe[:max_len] or "Video"

    @staticmethod
    def _is_url(token: str) -> bool:
        return token.startswith(("http://", "https://"))

    @staticmethod
    def _is_hls_url(url: str) -> bool:
        """
        Return True jika URL kemungkinan besar adalah HLS stream.
        Mencakup:
          • URL dengan path berakhiran .m3u8  (± query string)
          • HLS Proxy Rishort  → /hls/proxy?token=  (redirect ke CDN)
          • HLS M3U8 Rishort   → /hls/m3u8?token=   (direct M3U8 content)
          • Endpoint API Rishort/GoodShort    → /api/.../hls/...
        """
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path   = parsed.path.lower()
        query  = parsed.query.lower()
        host   = parsed.netloc.lower()

        if path.endswith(".m3u8"):
            return True
        # HLS Proxy Cloudflare Worker: /hls/proxy?token=... atau /hls/m3u8?token=...
        if ("workers.dev" in host and
                ("/hls/proxy" in path or "/hls/m3u8" in path) and
                "token=" in query):
            return True
        # Rishort API endpoint: /api/goodshort/hls/...
        if "rishort.com" in host and "/hls/" in path:
            return True
        return False

    @staticmethod
    def _extract_title_from_url(url: str) -> str:
        """
        Buat judul otomatis yang bermakna dari URL.

        Rishort API  → ambil ID episode dari path:
          /api/goodshort/hls/13160533/31001057214/playlist.m3u8?q=1080p
          → "Video_31001057214"

        HLS Proxy & HLS M3U8 → ambil 8 karakter pertama token:
          /hls/proxy?token=lASjlmtr...  → "Video_lASjlmtr"
          /hls/m3u8?token=DcuJq54z...   → "Video_DcuJq54z"

        Fallback     → stem dari path, atau "Video"
        """
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(url)
        path   = parsed.path

        # Rishort API: /api/goodshort/hls/{seriesId}/{episodeId}/playlist.m3u8
        m = re.search(r'/hls/(\d+)/(\d+)/', path)
        if m:
            return f"Video_{m.group(2)}"

        # HLS Proxy / HLS M3U8: ?token=xxxx...
        qs = parse_qs(parsed.query)
        if "token" in qs:
            token_prefix = qs["token"][0][:8]
            return f"Video_{token_prefix}"

        # Fallback: stem dari path
        stem = Path(path).stem
        if stem and stem not in ("proxy", "m3u8", "hls", "stream", "index", ""):
            return re.sub(r'[^a-zA-Z0-9_\-]', '', stem) or "Video"

        return "Video"

    @staticmethod
    def _detect_source_label(url: str) -> str:
        """Kembalikan label sumber yang ramah berdasarkan domain/path URL."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host   = parsed.netloc.lower()
        path   = parsed.path.lower()

        if "workers.dev" in host and "/hls/proxy" in path:
            return "HLS Proxy (Rishort)"
        if "workers.dev" in host and "/hls/m3u8" in path:
            return "HLS M3U8 (Rishort)"
        if "rishort.com" in host and "goodshort" in path:
            return "GoodShort via Rishort"
        if "rishort.com" in host:
            return "Rishort"
        if "goodshort" in host:
            return "GoodShort"
        if path.endswith(".m3u8"):
            return "HLS Stream"
        return "Stream"

    async def _safe_update(self, user_id: int, msg, text: str, reply_markup=None):
        """Edit pesan status; jika gagal kirim pesan baru."""
        if not self.uploader:
            return
        try:
            await self.uploader.bot.edit_message_text(
                chat_id=user_id,
                message_id=msg.message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        except Exception:
            try:
                await self.uploader.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
            except Exception:
                pass

    # =========================================================================
    # /l [judul] [link]  — single download dengan deteksi subtitle interaktif
    # =========================================================================
    async def link_download(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /l [judul] [link]   — judul kustom dari user
        /l [link]           — judul otomatis dari URL
        Contoh: /l Drama_Ep1 https://new.rishort.com/api/goodshort/hls/.../playlist.m3u8?q=1080p

        Alur:
          1. Validasi argumen & URL
          2. Deteksi subtitle dari playlist HLS
          3a. Ada subtitle  → simpan pending ke user_data → tanya user → state AWAITING_SUBTITLE_CHOICE
          3b. Tidak ada     → langsung mulai download sebagai background task
        """
        user_id = update.effective_user.id

        if ALLOWED_USERS and user_id not in ALLOWED_USERS:
            await update.message.reply_text("⛔ Anda tidak diizinkan menggunakan bot ini.")
            return ConversationHandler.END

        if not context.args:
            await update.message.reply_text(
                "❌ <b>Argumen tidak lengkap.</b>\n\n"
                "<b>Format:</b>\n"
                "  <code>/l [judul] [link]</code>\n"
                "  <code>/l [link]</code>\n\n"
                "<b>Contoh:</b>\n"
                "  <code>/l Drama_Ep1 https://new.rishort.com/api/goodshort/hls/13341728/31001069305/playlist.m3u8?q=1080p</code>",
                parse_mode="HTML"
            )
            return ConversationHandler.END

        args    = context.args
        url_idx = next((i for i, a in enumerate(args) if self._is_url(a)), None)

        if url_idx is None:
            await update.message.reply_text(
                "❌ <b>URL tidak ditemukan.</b>\n\n"
                "URL harus diawali dengan <code>http://</code> atau <code>https://</code>",
                parse_mode="HTML"
            )
            return ConversationHandler.END

        raw_url = args[url_idx].strip()

        if url_idx > 0:
            user_title = self._sanitize_filename(" ".join(args[:url_idx]))
        else:
            user_title = self._extract_title_from_url(raw_url)

        user_fmt = context.user_data.get("default_format", "mp4").lower()
        filename = f"{user_title}.{user_fmt}"

        if self.session_manager.has_active_session(user_id):
            await update.message.reply_text(
                "⚠️ Masih ada proses yang sedang berjalan.\n"
                "Tunggu hingga selesai atau ketik /cancel untuk membatalkan."
            )
            return ConversationHandler.END

        # ── Label sumber untuk pesan status ──────────────────────────────────
        source_label = self._detect_source_label(raw_url)

        if not self.uploader:
            self.uploader = TelegramUploader(context.bot)

        job_uuid    = uuid.uuid4().hex[:6]
        video_path  = DOWNLOAD_DIR / f"{user_id}_{user_title}_{job_uuid}.{user_fmt}"
        output_path = DOWNLOAD_DIR / f"{user_id}_{user_title}_{job_uuid}_out.{user_fmt}"

        # ── Deteksi subtitle dari playlist ───────────────────────────────────
        detect_msg = await update.message.reply_text(
            f"📥 <b>Download dimulai</b>\n"
            f"🎬 <b>Judul:</b> {user_title}\n"
            f"🔗 <b>Sumber:</b> {source_label}\n"
            f"⏳ Mengambil playlist dan segmen video...",
            parse_mode="HTML"
        )

        subtitle_tracks: List[dict] = []
        sub_url = ""
        try:
            # Gunakan download manager untuk deteksi subtitle
            raw_tracks = await self.download_manager.detect_hls_subtitles(raw_url)
            subtitle_tracks = [
                t for t in raw_tracks
                if not self._is_hls_url(t.get("uri", ""))
                and any(ext in t.get("uri", "").lower()
                        for ext in (".vtt", ".srt", ".ass", "subtitle", "sub"))
            ]
            if raw_tracks and not subtitle_tracks:
                logger.info(f"[/l] {len(raw_tracks)} track ditemukan tapi semua M3U8, diabaikan.")
            if subtitle_tracks:
                sub_url = subtitle_tracks[0].get("uri", "")
        except Exception as e:
            logger.warning(f"[/l] Gagal deteksi subtitle: {e}")

        if subtitle_tracks:
            # ── Ada subtitle → simpan pending, tanya user subtitle dulu ───
            context.user_data["pending_single"] = {
                "url":         raw_url,
                "title":       user_title,
                "filename":    filename,
                "subtitle_url": sub_url,
                "video_path":  str(video_path),
                "output_path": str(output_path),
                "status_msg":  detect_msg,
            }
            await detect_msg.edit_text(
                f"📥 <b>Download dimulai</b>\n"
                f"🎬 <b>Judul:</b> {user_title}\n"
                f"🔗 <b>Sumber:</b> {source_label}\n\n"
                f"💬 <b>Subtitle terdeteksi</b> untuk video ini.\n"
                f"Apakah subtitle ingin digabungkan?\n\n"
                f"1️⃣ Gabungkan subtitle ke video\n"
                f"2️⃣ Kirim video tanpa subtitle\n"
                f"3️⃣ Kirim subtitle sebagai file terpisah",
                parse_mode="HTML"
            )
            return AWAITING_SUBTITLE_CHOICE

        else:
            # ── Tidak ada subtitle → tampilkan konfirmasi format/quality ───
            # Cleanup session sementara (belum mulai download)
            self.session_manager.force_cleanup_session(user_id)

            sel_fmt = context.user_data.get("default_format", "mp4")
            sel_res = context.user_data.get("default_resolution", "1080p")
            context.user_data["conf_format"] = sel_fmt
            context.user_data["conf_resolution"] = sel_res

            # Simpan pending data untuk handle_confirmation_button
            context.user_data["pending_single_conf"] = {
                "url":           raw_url,
                "title":         user_title,
                "subtitle_url":  "",
                "subtitle_mode": "none",
            }

            markup = self._build_confirmation_keyboard(sel_fmt, sel_res)
            await detect_msg.edit_text(
                f"📋 <b>Konfirmasi Download</b>\n\n"
                f"🎬 <b>Judul:</b> {user_title}\n"
                f"🔗 <b>Sumber:</b> {source_label}\n\n"
                f"👇 <b>Pilih format dan kualitas, lalu tekan Mulai Download:</b>",
                parse_mode="HTML",
                reply_markup=markup
            )
            return ConversationHandler.END

    # =========================================================================
    # Handler jawaban subtitle untuk /l
    # =========================================================================
    async def handle_subtitle_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Menangkap jawaban '1', '2', atau '3' dari user setelah bot mendeteksi subtitle di /l.
        Kemudian melanjutkan download sesuai pilihan.
        """
        user_id = update.effective_user.id
        choice  = update.message.text.strip()

        subtitle_mode_map = {"1": "embed", "2": "none", "3": "separate"}
        subtitle_mode = subtitle_mode_map.get(choice)

        if subtitle_mode is None:
            await update.message.reply_text(
                "❓ Pilihan tidak valid. Ketik:\n"
                "1️⃣  1  — Gabungkan subtitle ke video\n"
                "2️⃣  2  — Tanpa subtitle\n"
                "3️⃣  3  — Kirim subtitle sebagai file terpisah"
            )
            return AWAITING_SUBTITLE_CHOICE

        pending = context.user_data.pop("pending_single", None)
        if not pending:
            await update.message.reply_text("❌ Data pending tidak ditemukan. Silakan ulangi perintah /l.")
            return ConversationHandler.END

        mode_label = {"embed": "Gabungkan subtitle", "none": "Tanpa subtitle", "separate": "Subtitle terpisah"}

        # Tampilkan konfirmasi format/quality
        sel_fmt = context.user_data.get("default_format", "mp4")
        sel_res = context.user_data.get("default_resolution", "1080p")
        context.user_data["conf_format"] = sel_fmt
        context.user_data["conf_resolution"] = sel_res

        context.user_data["pending_single_conf"] = {
            "url":           pending["url"],
            "title":         pending["title"],
            "subtitle_url":  pending.get("subtitle_url", ""),
            "subtitle_mode": subtitle_mode,
        }

        markup = self._build_confirmation_keyboard(sel_fmt, sel_res)
        await update.message.reply_text(
            f"📋 <b>Konfirmasi Download</b>\n\n"
            f"🎬 <b>Judul:</b> {pending['title']}\n"
            f"💬 <b>Mode subtitle:</b> {mode_label[subtitle_mode]}\n\n"
            f"👇 <b>Pilih format dan kualitas, lalu tekan Mulai Download:</b>",
            parse_mode="HTML",
            reply_markup=markup
        )
        return ConversationHandler.END

    # =========================================================================
    # /batch [judul] [link1] [link2] ...  — multi-episode, maks 100 link
    # =========================================================================
    async def batch_download(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /batch [judul_series] [link1] [link2] ... [linkN]   (maks 200 link)

        Alur:
          1. Validasi argumen & URL (maks 200)
          2. Pre-scan 3 link pertama untuk deteksi subtitle
          3a. Ada subtitle  → simpan pending → tanya user sekali → state AWAITING_BATCH_SUBTITLE
          3b. Tidak ada     → langsung mulai background task
        """
        user_id = update.effective_user.id

        if ALLOWED_USERS and user_id not in ALLOWED_USERS:
            await update.message.reply_text("⛔ Anda tidak diizinkan menggunakan bot ini.")
            return ConversationHandler.END

        if not context.args:
            await update.message.reply_text(
                "❌ <b>Argumen tidak lengkap.</b>\n\n"
                "<b>Format:</b>\n"
                "  <code>/batch [judul] [link1] [link2] ...</code>\n\n"
                "<b>Contoh:</b>\n"
                "  <code>/batch Drama https://new.rishort.com/.../31001069305/playlist.m3u8 https://new.rishort.com/.../31001069306/playlist.m3u8</code>\n\n"
                f"ℹ️ Maksimal {self.MAX_BATCH_EPISODES} link per perintah.",
                parse_mode="HTML"
            )
            return ConversationHandler.END

        args        = context.args
        urls        = [a for a in args if self._is_url(a)]
        title_parts = [a for a in args if not self._is_url(a)]

        if not urls:
            await update.message.reply_text(
                "❌ <b>Tidak ada URL yang ditemukan.</b>\n\n"
                "Pastikan setiap link diawali dengan <code>http://</code> atau <code>https://</code>",
                parse_mode="HTML"
            )
            return ConversationHandler.END

        if len(urls) > self.MAX_BATCH_EPISODES:
            await update.message.reply_text(
                f"⚠️ Maksimal <b>{self.MAX_BATCH_EPISODES} episode</b> dalam satu perintah "
                f"<code>/batch</code>.\n\n"
                f"Anda mengirim <b>{len(urls)} link</b>. "
                f"Silakan kirim batch berikutnya setelah batch pertama selesai.",
                parse_mode="HTML"
            )
            return ConversationHandler.END

        series_title = self._sanitize_filename(" ".join(title_parts)) if title_parts else "Series"
        total        = len(urls)

        if self.session_manager.has_active_session(user_id):
            await update.message.reply_text(
                "⚠️ Masih ada proses yang sedang berjalan.\n"
                "Tunggu hingga selesai atau ketik /cancel untuk membatalkan."
            )
            return ConversationHandler.END

        if not self.uploader:
            self.uploader = TelegramUploader(context.bot)

        # Preview episode (maks 10 baris)
        preview_lines = []
        for i, u in enumerate(urls[:10], start=1):
            short = u[:70] + ("..." if len(u) > 70 else "")
            preview_lines.append(f"  Ep{i:02d}: <code>{short}</code>")
        if total > 10:
            preview_lines.append(f"  <i>... dan {total - 10} episode lainnya</i>")

        status_msg = await update.message.reply_text(
            f"📥 <b>Batch download dimulai</b>\n\n"
            f"🎬 <b>Series:</b> {series_title}\n"
            f"📦 <b>Total Episode:</b> {total}\n\n"
            + "\n".join(preview_lines)
            + "\n\n⏳ Memeriksa subtitle...",
            parse_mode="HTML"
        )

        # ── Pre-scan subtitle (sample 3 link pertama) ─────────────────────────
        has_any_subtitle = False
        try:
            for sample_url in urls[:3]:
                raw_tracks = await self.download_manager.detect_hls_subtitles(sample_url)
                real_tracks = [
                    t for t in raw_tracks
                    if not self._is_hls_url(t.get("uri", ""))
                    and any(ext in t.get("uri", "").lower()
                            for ext in (".vtt", ".srt", ".ass", "subtitle", "sub"))
                ]
                if real_tracks:
                    has_any_subtitle = True
                    break
        except Exception as e:
            logger.warning(f"[/batch] Pre-scan subtitle gagal: {e}")

        if has_any_subtitle:
            # ── Ada subtitle → tanya user sekali ─────────────────────────
            context.user_data["pending_batch"] = {
                "urls":         urls,
                "series_title": series_title,
                "status_msg":   status_msg,
            }
            await status_msg.edit_text(
                f"📥 <b>Batch download dimulai</b>\n"
                f"🎬 <b>Series:</b> {series_title}\n"
                f"📦 <b>Total Episode:</b> {total}\n\n"
                f"💬 <b>Subtitle terdeteksi</b> pada beberapa episode.\n"
                f"Bagaimana subtitle ingin diproses?\n\n"
                f"1️⃣ Gabungkan semua subtitle ke video\n"
                f"2️⃣ Tanpa subtitle\n"
                f"3️⃣ Kirim subtitle terpisah",
                parse_mode="HTML"
            )
            return AWAITING_BATCH_SUBTITLE

        else:
            # ── Tidak ada subtitle → tampilkan konfirmasi format/quality ─
            sel_fmt = context.user_data.get("default_format", "mp4")
            sel_res = context.user_data.get("default_resolution", "1080p")
            context.user_data["conf_format"] = sel_fmt
            context.user_data["conf_resolution"] = sel_res

            context.user_data["pending_batch_link_conf"] = {
                "urls":          urls,
                "series_title":  series_title,
                "subtitle_mode": "none",
            }

            markup = self._build_confirmation_keyboard(sel_fmt, sel_res)
            await status_msg.edit_text(
                f"📋 <b>Konfirmasi Batch Download</b>\n\n"
                f"🎬 <b>Series:</b> {series_title}\n"
                f"📦 <b>Total Episode:</b> {total}\n\n"
                + "\n".join(preview_lines)
                + "\n\n👇 <b>Pilih format dan kualitas, lalu tekan Mulai Download:</b>",
                parse_mode="HTML",
                reply_markup=markup
            )
            return ConversationHandler.END

    # =========================================================================
    # Handler jawaban subtitle untuk /batch
    # =========================================================================
    async def handle_batch_subtitle_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Menangkap jawaban '1', '2', atau '3' dari user untuk pilihan subtitle batch.
        Pilihan ini akan diterapkan ke semua episode dalam batch.
        """
        user_id = update.effective_user.id
        choice  = update.message.text.strip()

        subtitle_mode_map = {"1": "embed", "2": "none", "3": "separate"}
        subtitle_mode = subtitle_mode_map.get(choice)

        if subtitle_mode is None:
            await update.message.reply_text(
                "❓ Pilihan tidak valid. Ketik:\n"
                "1️⃣  1  — Gabungkan semua subtitle ke video\n"
                "2️⃣  2  — Tanpa subtitle\n"
                "3️⃣  3  — Kirim subtitle terpisah"
            )
            return AWAITING_BATCH_SUBTITLE

        pending = context.user_data.pop("pending_batch", None)
        if not pending:
            await update.message.reply_text("❌ Data pending tidak ditemukan. Silakan ulangi perintah /batch.")
            return ConversationHandler.END

        mode_label = {"embed": "Gabungkan subtitle", "none": "Tanpa subtitle", "separate": "Subtitle terpisah"}
        total = len(pending["urls"])

        # Tampilkan konfirmasi format/quality
        sel_fmt = context.user_data.get("default_format", "mp4")
        sel_res = context.user_data.get("default_resolution", "1080p")
        context.user_data["conf_format"] = sel_fmt
        context.user_data["conf_resolution"] = sel_res

        context.user_data["pending_batch_link_conf"] = {
            "urls":          pending["urls"],
            "series_title":  pending["series_title"],
            "subtitle_mode": subtitle_mode,
        }

        markup = self._build_confirmation_keyboard(sel_fmt, sel_res)
        await update.message.reply_text(
            f"📋 <b>Konfirmasi Batch Download</b>\n\n"
            f"🎬 <b>Series:</b> {pending['series_title']}\n"
            f"📦 <b>Total Episode:</b> {total}\n"
            f"💬 <b>Mode subtitle:</b> {mode_label[subtitle_mode]}\n\n"
            f"👇 <b>Pilih format dan kualitas, lalu tekan Mulai Download:</b>",
            parse_mode="HTML",
            reply_markup=markup
        )
        return ConversationHandler.END

    # =========================================================================
    # Background task: process episodes (JSON flow) — dipanggil dari conf_ok
    # =========================================================================
    async def _process_episodes(
        self,
        user_id: int,
        selected_episodes: List[dict],
        drama_title: str,
        json_path: Path,
        is_batch: bool,
        status_msg,
        context: ContextTypes.DEFAULT_TYPE,
        output_format: str = "mp4",
        target_resolution: str = "1080p",
    ):
        """
        Proses download episode dari JSON flow.
        Dipanggil dari handle_confirmation_button setelah user tekan OK.
        """
        successful: int = 0
        failed: int = 0
        all_files_to_cleanup = []

        try:
            for idx, episode_data in enumerate(selected_episodes, 1):
                episode_num = episode_data['episode']
                episode_title = episode_data['title']
                video_url = episode_data['url']
                subtitle_url = episode_data.get('subtitle_url')

                # ── Pilih quality URL yang sesuai jika tersedia ────────────
                qualities_map = episode_data.get('qualities_map', {})
                if qualities_map:
                    import re as _re
                    _match = _re.search(r'(\d+)', target_resolution)
                    _target_h = int(_match.group(1)) if _match else 0
                    if _target_h > 0:
                        # Pastikan key adalah int untuk perbandingan
                        q_map_int = {int(k): v for k, v in qualities_map.items()}
                        available_heights = sorted(q_map_int.keys())
                        
                        if _target_h in q_map_int:
                            video_url = q_map_int[_target_h]
                            logger.info(f"[quality] Episode {episode_num}: exact match {_target_h}p")
                        else:
                            candidates = [h for h in available_heights if h <= _target_h]
                            if candidates:
                                best = max(candidates)
                                video_url = q_map_int[best]
                                logger.info(f"[quality] Episode {episode_num}: closest {best}p (target {_target_h}p)")
                            elif available_heights:
                                best = min(available_heights)
                                video_url = q_map_int[best]
                                logger.info(f"[quality] Episode {episode_num}: lowest available {best}p")

                try:
                    if is_batch:
                        await self.uploader.update_message(
                            user_id,
                            status_msg.message_id,
                            f"📥 <b>Downloading: {drama_title} — Ep {episode_num}</b>\n"
                            f"<i>{idx}/{len(selected_episodes)} episode</i>"
                        )
                except:
                    pass

                # Check for Vigloo cookies
                cookies = episode_data.get('cookies')

                safe_title = "".join(c for c in drama_title if c.isalnum() or c in ' ._-')[:30]
                safe_episode = f"EP{episode_num.zfill(2) if episode_num.isdigit() else episode_num}"
                user_fmt = output_format.lower()
                user_res = target_resolution
                
                # Gunakan user-specific directory
                user_dir = DOWNLOAD_DIR / str(user_id)
                user_dir.mkdir(parents=True, exist_ok=True)
                
                video_path = user_dir / f"{safe_title}_{safe_episode}.{user_fmt}"
                subtitle_path = user_dir / f"{safe_title}_{safe_episode}.srt"
                output_path = user_dir / f"{safe_title}_{safe_episode}_sub.{user_fmt}"

                episode_files = [video_path, subtitle_path, output_path]
                all_files_to_cleanup.extend(episode_files)

                try:
                    async def video_progress(current):
                        if not is_batch:
                            try:
                                await self.uploader.update_message(
                                    user_id,
                                    status_msg.message_id,
                                    f"📥 <b>Downloading: {drama_title} — Ep {episode_num}</b>\n"
                                    f"📦 Size: {format_size(current)}"
                                )
                            except:
                                pass

                    downloaded_video = await asyncio.wait_for(
                        self.download_manager.download_video(
                            url=video_url,
                            output_path=video_path,
                            user_id=user_id,
                            progress_callback=video_progress if not is_batch else None,
                            headers=cookies,  # Pass Vigloo cookies if present
                            subtitle_mode="none",
                            target_resolution=user_res,
                            output_format=user_fmt
                        ),
                        timeout=DOWNLOAD_TIMEOUT
                    )

                    if not downloaded_video:
                        failed += 1
                        logger.error(f"Download failed for episode {episode_num}")
                        await self.uploader.update_message(
                            user_id, status_msg.message_id,
                            f"❌ <b>Gagal: {drama_title} — Ep {episode_num}</b>\n"
                            f"<i>Download error - Episode dilewati</i>"
                        )
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
                            f"📥 <b>Downloading: {drama_title} — Subtitle Ep {episode_num}</b>"
                        )

                        subtitle_file = await self.download_manager.download_subtitle(
                            subtitle_url, subtitle_path
                        )

                        if subtitle_file:
                            logger.info(f"Subtitle downloaded for episode {episode_num}")
                    except Exception as e:
                        logger.warning(f"Subtitle download failed: {e}")
                        if subtitle_path.exists():
                            await FileCleanup.safe_delete(subtitle_path)

                # ── Tentukan mode subtitle per-episode ─────────────────────────
                ep_subtitle_mode = episode_data.get('subtitle_mode', 'separate')

                final_video = downloaded_video
                if subtitle_file:
                    if ep_subtitle_mode == "softsub":
                        try:
                            await self.uploader.update_message(
                                user_id,
                                status_msg.message_id,
                                f"🛠️ <b>Processing: {drama_title} — Softsub Ep {episode_num}</b>"
                            )
                            processed_video = await asyncio.wait_for(
                                self.video_processor.embed_softsub(
                                    downloaded_video, subtitle_file, output_path, user_id
                                ),
                                timeout=PROCESSING_TIMEOUT
                            )
                            if processed_video:
                                final_video = processed_video
                                logger.info(f"Softsub embedded for episode {episode_num}")
                            else:
                                logger.warning(f"Softsub embedding failed, using original video")
                                final_video = downloaded_video
                        except Exception as e:
                            logger.warning(f"Softsub embedding failed: {e}")
                            final_video = downloaded_video

                    elif ep_subtitle_mode == "embed":
                        try:
                            async def burn_progress(current):
                                if not is_batch:
                                    try:
                                        await self.uploader.update_message(
                                            user_id,
                                            status_msg.message_id,
                                            f"🛠️ <b>Processing: {drama_title} — Hardsub Ep {episode_num}</b>\n"
                                            f"📦 Size: {format_size(current)}"
                                        )
                                    except:
                                        pass

                            processed_video = await asyncio.wait_for(
                                self.video_processor.burn_subtitle(
                                    downloaded_video, subtitle_file, output_path, user_id, burn_progress, "id"
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

                    else:
                        logger.info(f"[sub-{ep_subtitle_mode}] Episode {episode_num}: video tanpa burn")

                # ── Remux ke MKV jika format MKV dan file belum MKV ────────────
                if user_fmt == "mkv" and final_video.exists() and final_video.suffix.lower() != ".mkv":
                    try:
                        mkv_path = final_video.with_suffix(".mkv")
                        await self.uploader.update_message(
                            user_id, status_msg.message_id,
                            f"🛠️ <b>Processing: {drama_title} — Remux MKV Ep {episode_num}</b>"
                        )
                        cmd = [
                            "ffmpeg", "-y", "-i", str(final_video),
                            "-c", "copy", str(mkv_path)
                        ]
                        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        await proc.communicate()
                        if proc.returncode == 0 and mkv_path.exists():
                            all_files_to_cleanup.append(mkv_path)
                            final_video = mkv_path
                            logger.info(f"Remuxed to MKV for episode {episode_num}")
                    except Exception as mkv_err:
                        logger.warning(f"Remux to MKV failed: {mkv_err}")

                # Cek ukuran file sebelum upload
                file_size_mb = final_video.stat().st_size / (1024 * 1024)
                logger.info(f"Final video size: {file_size_mb:.2f} MB")

                # ── Generate MediaInfo sebelum upload ─────────────────
                mi_markup = None
                try:
                    mi_url = await self.video_processor.generate_mediainfo_report(final_video)
                    if mi_url:
                        mi_markup = InlineKeyboardMarkup([
                            [InlineKeyboardButton("📊 MediaInfo", url=mi_url)]
                        ])
                except Exception as mi_err:
                    logger.warning(f"[mediainfo] Gagal generate report: {mi_err}")

                try:
                    async def ep_progress(current, total):
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
                        progress_callback=ep_progress,
                        reply_markup=mi_markup
                    )

                    if not upload_ok:
                        failed += 1
                        await self.uploader.update_message(
                            user_id, status_msg.message_id,
                            f"❌ <b>Gagal: {drama_title} — Ep {episode_num}</b>\n"
                            f"<i>Upload error - Episode dilewati</i>"
                        )
                        continue

                    successful += 1

                    # ── Kirim subtitle terpisah jika mode separate ────
                    if ep_subtitle_mode == "separate" and subtitle_file and subtitle_file.exists():
                        try:
                            safe_sub_name = f"{safe_title}_{safe_episode}.id.srt"
                            with open(subtitle_file, 'rb') as sub_f:
                                await context.bot.send_document(
                                    chat_id=user_id,
                                    document=sub_f,
                                    filename=safe_sub_name,
                                    caption=f"📝 <b>Subtitle Indonesia</b> — {drama_title} Ep {episode_num}",
                                    parse_mode="HTML"
                                )
                            logger.info(f"[sub-separate] Subtitle terkirim: {safe_sub_name}")
                        except Exception as sub_err:
                            logger.warning(f"[sub-separate] Gagal kirim subtitle: {sub_err}")


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
                    f"✅ <b>Selesai: {drama_title}</b>\n\n"
                    f"📦 Berhasil: {successful} episode\n"
                    f"❌ Gagal: {failed} episode\n\n"
                    f"<i>Seluruh file sementara telah dihapus.</i>"
                )
                try:
                    await self.uploader.update_message(user_id, status_msg.message_id, final_text)
                except:
                    await context.bot.send_message(chat_id=user_id, text=final_text, parse_mode="HTML")
            else:
                final_text = f"✅ <b>Selesai: {drama_title} — Ep {selected_episodes[0]['episode']}</b>"
                try:
                    await self.uploader.update_message(user_id, status_msg.message_id, final_text)
                except:
                    await context.bot.send_message(chat_id=user_id, text=final_text, parse_mode="HTML")

        except Exception as e:
            logger.error(f"Error in batch processing: {e}")
            try:
                await self.uploader.update_message(
                    user_id,
                    status_msg.message_id,
                    f"❌ <b>Gagal:</b> {str(e)}\n\n<i>Membersihkan file...</i>"
                )
            except:
                pass

        finally:
            await self._cleanup_user_session(user_id, context, "proses selesai")
            asyncio.create_task(FileCleanup.cleanup_old_files(DOWNLOAD_DIR, minutes=5))

    # =========================================================================
    # Background task: download SATU video dengan HLS optimization
    # =========================================================================
    async def _run_single_download(
        self,
        user_id: int,
        raw_url: str,
        video_path: Path,
        output_path: Path,
        status_msg,
        display_title: str = "Video",
        filename: str = "Video.mp4",
        subtitle_url: str = "",
        subtitle_mode: Literal["embed", "none", "separate"] = "none",
        cleanup_session: bool = True,
        batch_header: str = "",
        output_format: str = "mp4",
        target_resolution: str = "1080p",
    ) -> bool:
        """
        Download satu video → proses subtitle sesuai mode → upload → cleanup.
        subtitle_mode:
          'embed'    — burn subtitle ke dalam video (hard sub)
          'none'     — kirim video saja
          'separate' — kirim video + kirim file subtitle terpisah
        Return True jika sukses. Dipakai oleh /l dan setiap iterasi /batch.
        """
        downloaded_path: Optional[Path] = None
        subtitle_path: Optional[Path]   = None
        success = False

        try:
            # ── 1. Download video dengan HLS optimization ────────────────────
            is_hls_stream = self._is_hls_url(raw_url)

            async def dl_progress(current_bytes: int):
                try:
                    if is_hls_stream:
                        # Untuk HLS, current_bytes adalah jumlah segmen yang sudah di download
                        await self.uploader.update_message(
                            user_id, status_msg.message_id,
                            f"{batch_header}"
                            f"📥 <b>Downloading: {display_title}</b>\n"
                            f"📦 Progress: {current_bytes} segmen\n"
                            f"<i>💡 Optimizing with parallel download</i>"
                        )
                    else:
                        await self.uploader.update_message(
                            user_id, status_msg.message_id,
                            f"{batch_header}"
                            f"📥 <b>Downloading: {display_title}</b>\n"
                            f"📦 Size: {format_size(current_bytes)}"
                        )
                except Exception:
                    pass

            try:
                # Gunakan download manager dengan subtitle mode
                downloaded_path = await asyncio.wait_for(
                    self.download_manager.download_video(
                        url=raw_url,
                        output_path=video_path,
                        user_id=user_id,
                        progress_callback=dl_progress,
                        subtitle_mode=subtitle_mode,
                        subtitle_url=subtitle_url if subtitle_mode in ["embed", "separate"] else None,
                        target_resolution=target_resolution,
                        output_format=output_format
                    ),
                    timeout=DOWNLOAD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error(f"[dl] Timeout: {raw_url}")
                await self._safe_update(
                    user_id, status_msg,
                    f"{batch_header}❌ <b>{display_title}</b> — timeout.\n"
                    f"Coba lagi atau gunakan link yang lain."
                )
                return False

            if not downloaded_path or not downloaded_path.exists() or downloaded_path.stat().st_size == 0:
                logger.error(f"[dl] Gagal: {raw_url}")
                await self._safe_update(
                    user_id, status_msg,
                    f"{batch_header}❌ <b>Gagal: {display_title}</b>\n"
                    f"<i>Link tidak valid atau format tidak didukung.</i>"
                )
                return False

            file_size = downloaded_path.stat().st_size
            file_size_mb = file_size / (1024 * 1024)
            logger.info(f"[dl] OK: {display_title} ({format_size(file_size)})")
            
            if is_hls_stream:
                logger.info(f"[dl] HLS → MP4 selesai: {format_size(file_size)}")

            # ── 2. Cek ukuran ─────────────────────────────────────────────────
            if file_size > 2 * 1024 * 1024 * 1024:  # 2GB limit
                await self._safe_update(
                    user_id, status_msg,
                    f"⚠️ <b>{display_title}</b> — file terlalu besar!\n"
                    f"Ukuran: <b>{format_size(file_size)}</b> (batas Telegram: 2 GB)\n"
                    f"<i>File tidak dapat dikirim, dibersihkan.</i>"
                )
                await FileCleanup.safe_delete(downloaded_path, delay=2)
                return False

            # ── 3. Subtitle Handling ──────────────────────────────────────────
            if subtitle_mode in ("separate", "softsub", "embed"):
                # 1. Cek Local Subtitle Finder dulu (menggunakan display_title untuk tebak seri)
                from utils import LocalSubtitleFinder
                # Coba tebak drama_title dari display_title (biasanya "Drama Title EPxx")
                guessed_title = display_title.rsplit(' ', 1)[0]
                guessed_ep = display_title.rsplit(' ', 1)[-1].replace('Ep', '').replace('EP', '')
                
                local_sub = LocalSubtitleFinder.find_subtitle(guessed_title, guessed_ep)
                if local_sub:
                    subtitle_path = Path(local_sub).absolute()
                    logger.info(f"[dl] ✅ Found local subtitle: {subtitle_path.name}")
                
                # 2. Download from URL if local not found
                elif subtitle_url:
                    try:
                        await self._safe_update(
                            user_id, status_msg,
                            f"{batch_header}📥 <b>Downloading: {display_title} — Subtitle...</b>"
                        )
                        sub_ext = ".vtt" if ".vtt" in subtitle_url.lower() else ".srt"
                        sub_stem = video_path.stem
                        subtitle_path = (video_path.parent / f"{sub_stem}{sub_ext}").absolute()

                        subtitle_path = await self.download_manager.download_subtitle(
                            subtitle_url, subtitle_path
                        )
                    except Exception as e:
                        logger.warning(f"[dl] Subtitle download error: {e}")

            # ── 3b. Proses subtitle softsub/hardsub ───────────────────────────
            if subtitle_path and subtitle_path.exists():
                if subtitle_mode == "softsub":
                    try:
                        await self._safe_update(
                            user_id, status_msg,
                            f"{batch_header}🛠️ <b>Processing: {display_title} — Softsub...</b>"
                        )
                        # Gunakan output_path yang valid
                        processed = await self.video_processor.embed_softsub(
                            downloaded_path, subtitle_path, output_path, user_id
                        )
                        if processed and processed != downloaded_path:
                            downloaded_path = processed
                            logger.info(f"[dl] ✅ Softsub embedded: {display_title}")
                        else:
                            logger.warning(f"[dl] ❌ Softsub embedding gagal, fallback ke separate delivery")
                            subtitle_mode = "separate"
                    except Exception as e:
                        logger.warning(f"[dl] ❌ Softsub embedding failed: {e}")
                        subtitle_mode = "separate"
                elif subtitle_mode == "embed":
                    try:
                        await self._safe_update(
                            user_id, status_msg,
                            f"{batch_header}🛠️ <b>Processing: {display_title} — Hardsub...</b>"
                        )
                        processed = await self.video_processor.burn_subtitle(
                            downloaded_path, subtitle_path, output_path, user_id, None, "id"
                        )
                        if processed:
                            downloaded_path = processed
                            logger.info(f"[dl] ✅ Hardsub burned: {display_title}")
                    except Exception as e:
                        logger.warning(f"[dl] ❌ Hardsub burning failed: {e}")

            # ── 4. Rename ke nama file yang diinginkan user ───────────────────
            target_path = (downloaded_path.parent / filename).absolute()
            if downloaded_path != target_path:
                try:
                    if target_path.exists():
                        target_path.unlink()
                    downloaded_path.rename(target_path)
                    downloaded_path = target_path
                except Exception as e:
                    logger.warning(f"[dl] Gagal rename ke {filename}: {e}")

            # ── 5. Generate MediaInfo ──────────────────────────
            mi_markup = None
            try:
                mi_url = await self.video_processor.generate_mediainfo_report(downloaded_path)
                if mi_url:
                    mi_markup = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📊 MEDIA INFO", url=mi_url)]
                    ])
                    logger.info(f"[dl] MediaInfo link: {mi_url}")
            except Exception as mi_err:
                logger.warning(f"[dl] MediaInfo gagal: {mi_err}")

            # ── 6. Upload video ───────────────────────────────────────────────
            file_size = downloaded_path.stat().st_size
            file_size_mb = file_size / (1024 * 1024)
            
            upload_msg = f"{batch_header}📤 <b>Uploading: {display_title}</b>\n"
            upload_msg += f"📦 Size: {format_size(file_size)}"
            
            await self._safe_update(user_id, status_msg, upload_msg)

            async def up_progress(text: str):
                try:
                    await self.uploader.update_message(user_id, status_msg.message_id, text)
                except Exception:
                    pass

            upload_ok = await self.uploader.upload_with_progress(
                file_path=downloaded_path,
                chat_id=user_id,
                title=display_title,
                episode="",
                update_callback=up_progress,
                reply_markup=mi_markup,
            )

            if not upload_ok:
                logger.error(f"[dl] ❌ Upload gagal: {display_title}")
                await self._safe_update(
                    user_id, status_msg,
                    f"{batch_header}❌ <b>{display_title}</b> — upload gagal.\nSilakan coba lagi."
                )
                return False

            # ── 7. Kirim subtitle terpisah jika dipilih/fallback ───────────────────────
            if subtitle_mode == "separate" and subtitle_path and subtitle_path.exists():
                try:
                    sub_filename = Path(filename).stem + ".id" + subtitle_path.suffix
                    with open(subtitle_path, "rb") as sf:
                        await self.uploader.bot.send_document(
                            chat_id=user_id,
                            document=sf,
                            filename=sub_filename,
                            caption=f"📄 <b>Subtitle Indonesia:</b> {sub_filename}",
                            parse_mode="HTML",
                        )
                    logger.info(f"[dl] ✅ Subtitle terpisah dikirim: {sub_filename}")
                except Exception as e:
                    logger.warning(f"[dl] Gagal kirim subtitle terpisah: {e}")

            logger.info(f"[dl] ✅ Upload OK: {display_title}")
            if cleanup_session:
                await self._safe_update(
                    user_id, status_msg,
                    f"✅ <b>Selesai: {display_title}</b>\n\n"
                    f"<i>File sementara telah dihapus.</i>"
                )
            success = True

        except Exception as exc:
            logger.error(f"[dl] Error: {display_title}: {exc}", exc_info=True)
            await self._safe_update(
                user_id, status_msg,
                f"{batch_header}❌ <b>Gagal: {display_title}</b>\n"
                f"<code>General processing error.</code>"
            )

        finally:
            await FileCleanup.cleanup_episode_files(
                video_path=downloaded_path if (downloaded_path and downloaded_path.exists()) else None,
                subtitle_path=subtitle_path if (subtitle_path and subtitle_path.exists()) else None,
                output_path=output_path if output_path.exists() else None,
                delay=CLEANUP_DELAY,
            )
            if cleanup_session:
                self.session_manager.force_cleanup_session(user_id)
                logger.info(f"[dl] Session cleanup: user {user_id}")

        return success

    # =========================================================================
    # Background task: batch download (semua episode berurutan, maks 100)
    # =========================================================================
    async def _run_batch_download(
        self,
        user_id: int,
        urls: List[str],
        series_title: str,
        status_msg,
        subtitle_mode: Literal["embed", "none", "separate"] = "none",
        output_format: str = "mp4",
        target_resolution: str = "1080p",
    ):
        """
        Download semua episode secara berurutan.
        subtitle_mode diterapkan ke setiap episode (dipilih user sekali di awal).
        Jika episode gagal → tampilkan error, lanjut ke episode berikutnya.
        Kirim ringkasan lengkap di akhir.
        """
        total      = len(urls)
        successful: int = 0
        failed: int     = 0
        results: List[str] = []

        try:
            for idx, url in enumerate(urls, start=1):
                ep_label    = f"Ep{idx:02d}"
                safe_series = self._sanitize_filename(series_title)
                ep_filename  = f"{safe_series}_{ep_label}.{output_format}"
                job_uuid    = uuid.uuid4().hex[:6]
                user_dir    = DOWNLOAD_DIR / str(user_id)
                user_dir.mkdir(parents=True, exist_ok=True)
                video_path  = (user_dir / f"{safe_series}_{ep_label}_{job_uuid}.{output_format}").absolute()
                output_path = (user_dir / f"{safe_series}_{ep_label}_{job_uuid}_out.{output_format}").absolute()

                # ── Deteksi subtitle untuk episode ini ───────────────────────
                episode_subtitle_url = ""
                if subtitle_mode in ("embed", "separate"):
                    try:
                        raw_tracks = await self.download_manager.detect_hls_subtitles(url)
                        real_tracks = [
                            t for t in raw_tracks
                            if not self._is_hls_url(t.get("uri", ""))
                            and any(ext in t.get("uri", "").lower()
                                    for ext in (".vtt", ".srt", ".ass", "subtitle", "sub"))
                        ]
                        if real_tracks:
                            episode_subtitle_url = real_tracks[0].get("uri", "")
                    except Exception as e:
                        logger.warning(f"[batch] Deteksi subtitle ep{idx} gagal: {e}")

                batch_header = (
                    f"🛠️ <b>Processing Batch: {series_title}</b>\n"
                    f"📦 Progress: <b>{idx}/{total}</b> episode\n\n"
                )

                await self._safe_update(user_id, status_msg, batch_header)

                ok = await self._run_single_download(
                    user_id=user_id,
                    raw_url=url,
                    video_path=video_path,
                    output_path=output_path,
                    status_msg=status_msg,
                    display_title=f"{series_title} {ep_label}",
                    filename=ep_filename,
                    subtitle_url=episode_subtitle_url,
                    subtitle_mode=subtitle_mode,
                    cleanup_session=False,
                    batch_header=batch_header,
                    output_format=output_format,
                    target_resolution=target_resolution
                )

                if ok:
                    successful += 1
                    results.append(f"✅ {ep_filename} selesai")
                else:
                    failed += 1
                    results.append(f"❌ {ep_filename} gagal")

                if idx < total:
                    await asyncio.sleep(2)

            # ── Laporan akhir ─────────────────────────────────────────────────
            MAX_LINES = 50
            shown   = results[:MAX_LINES]
            if len(results) > MAX_LINES:
                shown.append(f"<i>... dan {len(results) - MAX_LINES} episode lainnya</i>")

            icon   = "✅" if failed == 0 else ("⚠️" if successful > 0 else "❌")
            status = "Semua berhasil!" if failed == 0 else f"{successful} berhasil, {failed} gagal"

            final_text = (
                f"✅ <b>Selesai: {series_title}</b>\n\n"
                f"📊 <b>Ringkasan:</b>\n"
                f"• Total: {total} episode\n"
                f"• Berhasil: {successful}\n"
                f"• Gagal: {failed}\n\n"
                + "\n".join(shown)
                + "\n\n<i>Seluruh file sementara telah dihapus.</i>"
            )
            await self._safe_update(user_id, status_msg, final_text)

        except Exception as exc:
            logger.error(f"[batch] Error: {exc}", exc_info=True)
            await self._safe_update(
                user_id, status_msg,
                f"❌ <b>Batch download terhenti.</b>\n<code>{str(exc)[:300]}</code>"
            )

        finally:
            self.session_manager.force_cleanup_session(user_id)
            asyncio.create_task(FileCleanup.cleanup_old_files(DOWNLOAD_DIR, minutes=5))
            logger.info(f"[batch] Selesai user {user_id}: {successful}/{total} OK")
    
    async def handle_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle direct JSON URLs."""
        user_id = update.effective_user.id
        url = update.message.text.strip()
        
        if not url.startswith(("http://", "https://")):
            return
            
        status_msg = await update.message.reply_text("🛠️ <b>Processing: Fetching metadata...</b>", parse_mode="HTML")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        await status_msg.edit_text(f"❌ <b>Gagal mengambil data (HTTP {resp.status})</b>", parse_mode="HTML")
                        return
                    data = await resp.json()
            
            # Save to temp file
            temp_path = DOWNLOAD_DIR / f"{user_id}_temp_{uuid.uuid4().hex[:6]}.json"
            async with aiofiles.open(temp_path, 'w', encoding='utf-8') as f:
                import json as _json
                await f.write(_json.dumps(data))
            
            # Spoof a document update for handle_json_file
            from types import SimpleNamespace
            update.message.document = SimpleNamespace(file_id='url_fetched', file_name='url_data.json')
            # We need to bypass the actual download in handle_json_file if it's from URL
            context.user_data['url_fetched_path'] = str(temp_path)
            
            return await self.handle_json_file(update, context)
            
        except Exception as e:
            logger.error(f"Error fetching URL: {e}")
            await status_msg.edit_text(f"❌ <b>Error:</b> {str(e)}", parse_mode="HTML")

    async def handle_callback_mi(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle MediaInfo button click."""
        query = update.callback_query
        await query.answer()
        
        # mi|filename
        _, filename = query.data.split("|", 1)
        file_path = DOWNLOAD_DIR / filename
        
        if not file_path.exists():
            await query.edit_message_caption(
                caption=query.message.caption + "\n\n⚠️ <i>File MediaInfo sudah tidak tersedia di server.</i>",
                parse_mode="HTML"
            )
            return
            
        try:
            info_str = await self.video_processor.get_detailed_mediainfo_string(file_path)
            
            # Add close button
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Tutup MediaInfo", callback_data="close_mi")]
            ])
            
            # Simpan caption asli untuk restorasi nanti
            context.user_data[f"orig_cap_{query.message.message_id}"] = query.message.caption
            
            await query.edit_message_caption(
                caption=f"📊 <b>Detailed Media Information:</b>\n\n<code>{info_str}</code>",
                reply_markup=markup,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Error getting mediainfo: {e}")
            await query.answer("Gagal mengambil Media Info.", show_alert=True)

    async def handle_close_mi(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Restore original caption after closing MediaInfo."""
        query = update.callback_query
        await query.answer()
        
        orig_cap = context.user_data.get(f"orig_cap_{query.message.message_id}", "Video download selesai.")
        
        # Restore original mi button if possible
        # We don't have the original filename easily here unless we store it
        # but we can just restore the caption for now.
        
        await query.edit_message_caption(
            caption=orig_cap,
            parse_mode="HTML"
        )

    async def _cleanup_user_session(self, user_id, context, reason):
        """Cleanup user session and ALL temp files immediately."""
        logger.info(f"🧹 Absolute cleanup for user {user_id} (Reason: {reason})")
        self.session_manager.force_cleanup_session(user_id)
        
        # Force delete user directory
        user_dir = DOWNLOAD_DIR / str(user_id)
        if user_dir.exists():
            await FileCleanup.safe_delete(user_dir, delay=0)
            logger.info(f"🧹 Force deleted session directory: {user_dir}")

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
            
        logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)
        
        # Try to cleanup user files and session
        if update and update.effective_user:
            user_id = update.effective_user.id
            await self._cleanup_user_session(user_id, context, f"error handler: {context.error}")
        
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ <b>Gagal: Terjadi kesalahan sistem</b>\n\n"
                    "<i>Seluruh file sementara dan session telah dibersihkan. Silakan coba lagi.</i>",
                    parse_mode="HTML"
                )
            except:
                pass

def main():
    """Main entry point"""
    bot = DownloaderBot()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            # Alur JSON
            MessageHandler(filters.Document.FileExtension("json"), bot.handle_json_file),
            # Alur URL JSON
            MessageHandler(filters.TEXT & filters.Entity("url"), bot.handle_url),
            # Alur /l dan /batch
            CommandHandler("l",     bot.link_download),
            CommandHandler("batch", bot.batch_download),
        ],
        states={
            # ── Alur JSON ────────────────────────────────────────────────────
            AWAITING_BATCH_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_batch_choice)
            ],
            AWAITING_CONFIRMATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.process_video)
            ],
            # ── Dramawave — pilih mode subtitle ──────────────────────────────
            AWAITING_DRAMAWAVE_SUBTITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_dramawave_subtitle_choice)
            ],
            # ── Format JSON lain dengan subtitle terpisah ─────────────────────
            AWAITING_SOFTSUB_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_softsub_choice)
            ],
            # ── Alur /l — tanya subtitle single ──────────────────────────────
            AWAITING_SUBTITLE_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_subtitle_choice)
            ],
            # ── Alur /batch — tanya subtitle batch ───────────────────────────
            AWAITING_BATCH_SUBTITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_batch_subtitle_choice)
            ],
        },
        fallbacks=[CommandHandler("cancel", bot.cancel)],
        name="download_conversation",
        persistent=False
    )
    
    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("help",  bot.help))
    app.add_handler(CommandHandler("cancel", bot.cancel))
    app.add_handler(CallbackQueryHandler(bot.handle_settings_choice, pattern="^set_"))
    app.add_handler(CallbackQueryHandler(bot.handle_confirmation_button, pattern="^conf_"))
    app.add_handler(CallbackQueryHandler(bot.handle_callback_mi, pattern="^mi\|"))
    app.add_handler(CallbackQueryHandler(bot.handle_close_mi, pattern="^close_mi$"))
    app.add_handler(conv_handler)
    app.add_error_handler(bot.error_handler)
    
    print("=" * 60)
    print("🚀 Bot Downloader Telegram - HLS Stream + Subtitle Interaktif")
    print("=" * 60)
    print("Fitur:")
    print("Fitur:")
    print("✅ Auto detect semua episode (termasuk dramabox v2)")
    print("✅ Auto detect subtitle Indonesia (id, ind, bahasa, dll)")
    print("✅ Hard subtitle otomatis (JSON flow)")
    print("✅ Deteksi subtitle HLS + pilihan embed/none/separate (/l & /batch)")
    print("✅ Batch download via JSON")
    print("✅ Auto cleanup files setelah selesai")
    print("✅ Auto hapus session jika error")
    print("✅ /l [judul] [link]  — Download single video dengan judul kustom")
    print("✅ /batch [judul] [link1] [link2] ... — Batch download multi-episode")
    print("✅ Mendukung Rishort, GoodShort, HLS Proxy")
    print("✅ Kompresi otomatis < 50MB untuk Telegram")
    print("✅ Merge video + audio terpisah otomatis")
    print("✅ Parallel segment download untuk HLS")
    print("✅ Auto refresh token HLS jika expired")
    print("-" * 60)
    print(f"Bot Token: {BOT_TOKEN[:10]}...{BOT_TOKEN[-5:]}")
    print(f"Download Directory: {DOWNLOAD_DIR.absolute()}")
    print(f"Auto Cleanup: {'ON' if DELETE_AFTER_UPLOAD else 'OFF'}")
    print(f"Cleanup Delay: {CLEANUP_DELAY} seconds")
    print(f"Max Concurrent Downloads: {MAX_CONCURRENT_DOWNLOADS}")
    print(f"Session Timeout: {SESSION_TIMEOUT} seconds")
    print(f"Target File Size: {TARGET_FILE_SIZE_MB} MB")
    print("=" * 60)
    print("Bot started! Press Ctrl+C to stop.")
    print("=" * 60)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()