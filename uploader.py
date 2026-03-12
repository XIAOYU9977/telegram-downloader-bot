import asyncio
from pathlib import Path
from typing import Optional, Callable
import logging
from telegram import Bot, InputFile
from telegram.constants import ParseMode
from telegram.error import TimedOut, RetryAfter, NetworkError

from utils import ProgressTracker, format_size, format_speed, logger
from config import CHUNK_SIZE, MAX_CONCURRENT_UPLOADS, MAX_RETRIES, API_ID, API_HASH, BOT_TOKEN
import time

class TelegramUploader:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.upload_limiter = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)
        self.max_retries = MAX_RETRIES
        self.pyrogram_app = None
        
        if API_ID and API_HASH:
            try:
                from pyrogram import Client
                self.pyrogram_app = Client(
                    "fast_uploader",
                    api_id=API_ID,
                    api_hash=API_HASH,
                    bot_token=BOT_TOKEN,
                    in_memory=True, # No need for session file since we use Bot Token
                    workers=4
                )
                logger.info("🚀 Pyrogram client ready for ultra-fast uploads.")
            except ImportError:
                logger.warning("Pyrogram is not installed. Defaulting to standard upload.")
        
    async def upload_video(self, file_path: Path, chat_id: int, title: str, episode: str,
                          progress_callback: Optional[Callable] = None,
                          reply_markup=None) -> bool:
        """Upload video to Telegram with progress tracking and retry"""
        for attempt in range(self.max_retries):
            try:
                async with self.upload_limiter:
                    file_size = file_path.stat().st_size
                    logger.info(f"Starting upload: {file_path} ({format_size(file_size)}) - Attempt {attempt + 1}")
                    
                    if file_size > 2 * 1024 * 1024 * 1024:
                        logger.error(f"File too large: {file_size} bytes")
                        return False
                    
                    # Cek if Pyrogram is enabled for FAST MTProto upload
                    if self.pyrogram_app:
                        if not getattr(self.pyrogram_app, "is_connected", False):
                            await self.pyrogram_app.start()
                            
                        last_update = [0]
                        async def pyrogram_progress(current, total):
                            now = time.time()
                            if now - last_update[0] > 2.0 or current == total:
                                last_update[0] = now
                                if progress_callback:
                                    try:
                                        await progress_callback(current, total)
                                    except Exception:
                                        pass
                                        
                        caption = f"🎬 **{title}**\n"
                        if episode:
                            caption += f"📺 Episode: {episode}\n"
                            
                        # Convert ptb reply_markup to Pyrogram format
                        pyro_markup = None
                        if reply_markup:
                            try:
                                from pyrogram.types import InlineKeyboardMarkup as PyroMarkup
                                from pyrogram.types import InlineKeyboardButton as PyroButton
                                pyro_buttons = []
                                for row in reply_markup.inline_keyboard:
                                    pyro_row = []
                                    for btn in row:
                                        if btn.url:
                                            pyro_row.append(PyroButton(btn.text, url=btn.url))
                                        elif btn.callback_data:
                                            pyro_row.append(PyroButton(btn.text, callback_data=btn.callback_data))
                                    pyro_buttons.append(pyro_row)
                                pyro_markup = PyroMarkup(pyro_buttons)
                            except Exception:
                                pyro_markup = None

                        is_mkv = file_path.suffix.lower() == ".mkv"
                        if is_mkv:
                            await self.pyrogram_app.send_document(
                                chat_id=chat_id,
                                document=str(file_path),
                                caption=caption,
                                progress=pyrogram_progress,
                                reply_markup=pyro_markup,
                                force_document=True
                            )
                        else:
                            await self.pyrogram_app.send_video(
                                chat_id=chat_id,
                                video=str(file_path),
                                caption=caption,
                                progress=pyrogram_progress,
                                reply_markup=pyro_markup
                            )
                        logger.info(f"🚀 Pyrogram Upload completed for {file_path} ({'MKV doc' if is_mkv else 'MP4 video'})")
                        return True

                    # Fallback to python-telegram-bot HTTP standard upload
                    tracker = ProgressTracker(file_size, progress_callback)
                    await tracker.start()
                    
                    is_mkv = file_path.suffix.lower() == ".mkv"
                    with open(file_path, 'rb') as video_file:
                        caption = f"🎬 <b>{title}</b>\n"
                        if episode:
                            caption += f"📺 Episode: {episode}\n"
                        
                        if is_mkv:
                            await self.bot.send_document(
                                chat_id=chat_id,
                                document=InputFile(video_file, filename=file_path.name),
                                caption=caption,
                                parse_mode=ParseMode.HTML,
                                read_timeout=600,
                                write_timeout=600,
                                connect_timeout=600,
                                pool_timeout=600,
                                reply_markup=reply_markup
                            )
                        else:
                            await self.bot.send_video(
                                chat_id=chat_id,
                                video=InputFile(video_file, filename=file_path.name),
                                caption=caption,
                                supports_streaming=True,
                                parse_mode=ParseMode.HTML,
                                read_timeout=600,
                                write_timeout=600,
                                connect_timeout=600,
                                pool_timeout=600,
                                reply_markup=reply_markup
                            )
                        
                    logger.info(f"Upload completed for {file_path}")
                    return True
                    
            except TimedOut:
                logger.warning(f"Upload timeout for {file_path} (attempt {attempt + 1})")
                if attempt < self.max_retries - 1:
                    wait_time = (attempt + 1) * 5
                    logger.info(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Upload failed after {self.max_retries} attempts")
                    return False
                    
            except RetryAfter as e:
                logger.warning(f"Rate limited. Waiting {e.retry_after} seconds")
                await asyncio.sleep(e.retry_after)
                
            except NetworkError as e:
                logger.error(f"Network error during upload: {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(5)
                else:
                    return False
                    
            except Exception as e:
                logger.error(f"Upload failed for {file_path}: {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(5)
                else:
                    return False
        
        return False
    
    async def upload_with_progress(self, file_path: Path, chat_id: int, title: str, episode: str,
                                  update_callback: Callable, reply_markup=None) -> bool:
        """Upload with progress updates"""
        async def progress_callback(current, total):
            percentage = (current / total) * 100
            speed = format_speed(current / (1 if current > 0 else 1))
            try:
                await update_callback(
                    f"📤 <b>Uploading...</b>\n"
                    f"Progress: {percentage:.1f}%\n"
                    f"Size: {format_size(current)}/{format_size(total)}\n"
                    f"Speed: {speed}"
                )
            except Exception as e:
                logger.warning(f"Failed to update progress: {e}")
            
        return await self.upload_video(file_path, chat_id, title, episode, progress_callback, reply_markup=reply_markup)
    
    async def send_error(self, chat_id: int, error_msg: str):
        """Send error message to user with retry"""
        for attempt in range(self.max_retries):
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ <b>Error</b>\n{error_msg}",
                    parse_mode=ParseMode.HTML,
                    read_timeout=30,
                    write_timeout=30,
                    connect_timeout=30
                )
                return
            except Exception as e:
                logger.warning(f"Failed to send error message (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2)
    
    async def send_status(self, chat_id: int, message: str):
        """Send status message with retry"""
        for attempt in range(self.max_retries):
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    read_timeout=30,
                    write_timeout=30,
                    connect_timeout=30
                )
                return
            except Exception as e:
                logger.warning(f"Failed to send status (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2)
    
    async def update_message(self, chat_id: int, message_id: int, text: str):
        """Edit existing message with retry mechanism"""
        for attempt in range(self.max_retries):
            try:
                await self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    read_timeout=30,
                    write_timeout=30,
                    connect_timeout=30
                )
                return
            except TimedOut:
                logger.warning(f"Timeout updating message (attempt {attempt + 1})")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2)
            except Exception as e:
                if "not modified" in str(e).lower():
                    return
                logger.warning(f"Failed to update message (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2)
                else:
                    try:
                        await self.bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode=ParseMode.HTML
                        )
                    except:
                        pass