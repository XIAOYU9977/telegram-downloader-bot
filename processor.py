import asyncio
import subprocess
import functools
from pathlib import Path
from typing import Optional, Callable, Dict, Any, List
import logging
import os
import aiofiles
import chardet
import aiohttp
import json as _json

from utils import logger
from config import (
    FFMPEG_THREADS,
    VIDEO_CODEC, VIDEO_PROFILE, VIDEO_LEVEL, VIDEO_PRESET, VIDEO_TUNE,
    PIX_FMT, COLOR_PRIMARIES, COLOR_TRC, COLORSPACE, COLOR_RANGE,
    TARGET_VIDEO_BITRATE, TARGET_VIDEO_MAXRATE, TARGET_VIDEO_BUFSIZE,
    TARGET_FPS, TARGET_WIDTH, TARGET_HEIGHT, X264_PARAMS,
    AUDIO_CODEC, TARGET_AUDIO_BITRATE, AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE, AUDIO_CHANNEL_LAYOUT,
    OUTPUT_FORMAT, MOVFLAGS,
    ENABLE_AUTO_COMPRESS, MIN_BITRATE, MAX_BITRATE,
    COMPRESSION_ATTEMPTS, TARGET_FILE_SIZE_MB
)

from task_tracker import TaskTracker

class VideoProcessor:
    def __init__(self, task_tracker: Optional[TaskTracker] = None):
        self.task_tracker = task_tracker
        self.processing_tasks = {}

    # ------------------------------------------------------------------ #
    #  ENCODE – Main encode dengan semua target specs                      #
    # ------------------------------------------------------------------ #
    async def encode_video(self, input_path: Path, output_path: Path,
                           user_id: int,
                           progress_callback: Optional[Callable] = None) -> Optional[Path]:
        """
        Encode video ke spec target:
          720x1280 | 25fps CFR | H.264 High@3.1 | 382kbps ABR
          AAC-LC stereo 48kHz 132kbps | BT.709 Limited | MP4 faststart
        """
        logger.info(f"🎬 Encoding: {input_path.name} → {output_path.name}")

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            # Video stream
            "-vf", (
                f"scale={TARGET_WIDTH}:{TARGET_HEIGHT}:flags=lanczos,"
                f"fps={TARGET_FPS}"
            ),
            "-c:v", VIDEO_CODEC,
            "-profile:v", VIDEO_PROFILE,
            "-level:v", VIDEO_LEVEL,
            "-preset", VIDEO_PRESET,
            "-tune", VIDEO_TUNE,
            "-b:v", TARGET_VIDEO_BITRATE,
            "-maxrate", TARGET_VIDEO_MAXRATE,
            "-bufsize", TARGET_VIDEO_BUFSIZE,
            "-pix_fmt", PIX_FMT,
            # Color metadata
            "-color_primaries", COLOR_PRIMARIES,
            "-color_trc", COLOR_TRC,
            "-colorspace", COLORSPACE,
            "-color_range", COLOR_RANGE,
            # x264 exact params
            "-x264-params", X264_PARAMS,
            # Audio stream
            "-c:a", AUDIO_CODEC,
            "-b:a", TARGET_AUDIO_BITRATE,
            "-ac", str(AUDIO_CHANNELS),
            "-ar", str(AUDIO_SAMPLE_RATE),
            "-channel_layout", AUDIO_CHANNEL_LAYOUT,
            # Container
            "-movflags", MOVFLAGS,
            "-threads", str(FFMPEG_THREADS),
            str(output_path)
        ]

        result = await self._run_ffmpeg(cmd, input_path.parent, output_path, user_id, progress_callback)

        # Auto compress jika masih > TARGET_FILE_SIZE_MB
        if result and ENABLE_AUTO_COMPRESS:
            size_mb = result.stat().st_size / (1024 * 1024)
            if size_mb > TARGET_FILE_SIZE_MB:
                logger.warning(f"⚠️ Output {size_mb:.1f}MB > {TARGET_FILE_SIZE_MB}MB, compressing...")
                result = await self.compress_to_target(result, output_path.with_suffix('.compressed.mp4'),
                                                       user_id, progress_callback)
        return result

    # ------------------------------------------------------------------ #
    #  COMPRESS – Turunkan bitrate agar muat di Telegram                  #
    # ------------------------------------------------------------------ #
    async def compress_to_target(self, input_path: Path, output_path: Path,
                                  user_id: int,
                                  progress_callback: Optional[Callable] = None) -> Optional[Path]:
        """Kompres video sampai di bawah TARGET_FILE_SIZE_MB"""
        duration = await self._get_duration(input_path)
        if not duration:
            logger.error("❌ Cannot get video duration for compression")
            return input_path

        for attempt in range(COMPRESSION_ATTEMPTS):
            # Hitung bitrate yang diperlukan
            target_size_bits = TARGET_FILE_SIZE_MB * 8 * 1024 * 1024 * 0.95
            audio_bits = int(TARGET_AUDIO_BITRATE.replace('k', '')) * 1000 * duration
            video_bits = target_size_bits - audio_bits
            calc_bitrate = max(
                int(MIN_BITRATE.replace('k', '')),
                min(
                    int(MAX_BITRATE.replace('k', '')),
                    int(video_bits / duration / 1000)
                )
            )
            bitrate_str = f"{calc_bitrate}k"
            logger.info(f"🗜️ Compress attempt {attempt+1}: video={bitrate_str}")

            cmd = [
                "ffmpeg", "-y",
                "-i", str(input_path),
                "-c:v", VIDEO_CODEC,
                "-profile:v", VIDEO_PROFILE,
                "-level:v", VIDEO_LEVEL,
                "-preset", "fast",
                "-b:v", bitrate_str,
                "-maxrate", bitrate_str,
                "-bufsize", f"{calc_bitrate * 2}k",
                "-pix_fmt", PIX_FMT,
                "-color_primaries", COLOR_PRIMARIES,
                "-color_trc", COLOR_TRC,
                "-colorspace", COLORSPACE,
                "-color_range", COLOR_RANGE,
                "-c:a", AUDIO_CODEC,
                "-b:a", TARGET_AUDIO_BITRATE,
                "-ac", str(AUDIO_CHANNELS),
                "-ar", str(AUDIO_SAMPLE_RATE),
                "-movflags", MOVFLAGS,
                "-threads", str(FFMPEG_THREADS),
                str(output_path)
            ]

            result = await self._run_ffmpeg(cmd, input_path.parent, output_path, user_id, progress_callback)
            if result:
                size_mb = result.stat().st_size / (1024 * 1024)
                logger.info(f"📦 Compressed: {size_mb:.1f}MB")
                if size_mb <= TARGET_FILE_SIZE_MB:
                    return result

        logger.warning("⚠️ Could not compress below target, returning best result")
        return output_path if output_path.exists() else input_path

    # ------------------------------------------------------------------ #
    #  SOFTSUB – Embed subtitle sebagai track soft (bisa dimatikan)       #
    # ------------------------------------------------------------------ #
    async def embed_softsub(self, video_path: Path, subtitle_path: Path,
                             output_path: Path, user_id: int) -> Optional[Path]:
        """
        Embed subtitle sebagai soft subtitle track.
        """
        try:
            logger.info(f"💬 Embedding softsub: {subtitle_path.name} → {video_path.name}")

            if not video_path.exists():
                logger.error(f"❌ Video not found: {video_path}")
                return video_path
            if not subtitle_path.exists():
                logger.error(f"❌ Subtitle not found: {subtitle_path}")
                return video_path

            srt_path = await self._fix_encoding(subtitle_path)
            if srt_path.suffix.lower() == ".vtt":
                converted = await self._convert_vtt_to_srt(srt_path)
                if converted:
                    srt_path = converted

            logger.info(f"💬 SRT ready: {srt_path.name}")

            # ── MP4 Method 1: metadata bahasa + disposition default ──────────
            cmd1 = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", str(srt_path),
                "-map", "0:v:0",
                "-map", "0:a?",          # opsional — tidak gagal jika tidak ada audio
                "-map", "1:0",
                "-c:v", "copy",
                "-c:a", "copy",
                "-c:s", "mov_text",
                "-metadata:s:s:0", "language=ind",
                "-metadata:s:s:0", "title=Indonesian",
                "-disposition:s:0", "default",
                "-movflags", MOVFLAGS,
                str(output_path),
            ]
            result = await self._run_ffmpeg(cmd1, video_path.parent, output_path, user_id)
            if result:
                return result

            # ── MP4 Method 2: tanpa metadata ────────────────────────────────
            cmd2 = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", str(srt_path),
                "-map", "0:v",
                "-map", "0:a?",
                "-map", "1:0",
                "-c:v", "copy",
                "-c:a", "copy",
                "-c:s", "mov_text",
                "-movflags", MOVFLAGS,
                str(output_path),
            ]
            result = await self._run_ffmpeg(cmd2, video_path.parent, output_path, user_id)
            if result:
                return result

            # ── MP4 Method 3: tanpa map explicit, subtitle original ──────────
            cmd3 = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", str(subtitle_path),
                "-c:v", "copy",
                "-c:a", "copy",
                "-c:s", "mov_text",
                "-movflags", MOVFLAGS,
                str(output_path),
            ]
            result = await self._run_ffmpeg(cmd3, video_path.parent, output_path, user_id)
            if result:
                return result

            # ── MKV Fallback: SRT native, tidak butuh mov_text ──────────────
            logger.warning("⚠️ Semua MP4 method gagal, mencoba MKV fallback...")
            mkv_output = output_path.with_suffix(".mkv")
            cmd4 = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", str(srt_path),
                "-map", "0:v",
                "-map", "0:a?",
                "-map", "1:0",
                "-c:v", "copy",
                "-c:a", "copy",
                "-c:s", "srt",
                "-metadata:s:s:0", "language=ind",
                "-disposition:s:0", "default",
                str(mkv_output),
            ]
            result = await self._run_ffmpeg(cmd4, video_path.parent, mkv_output, user_id)
            if result:
                return result

            # Semua gagal — kembalikan video original agar upload tetap jalan
            logger.error("❌ Semua softsub method gagal, mengirim video tanpa subtitle")
            return video_path

        except Exception as e:
            logger.error(f"❌ embed_softsub exception: {e}")
            return video_path  # selalu kembalikan sesuatu agar bot tidak crash

    # ------------------------------------------------------------------ #
    #  BURN SUBTITLE – Dengan 4 fallback methods                          #
    # ------------------------------------------------------------------ #
    async def burn_subtitle(self, video_path: Path, subtitle_path: Path,
                            output_path: Path,
                            user_id: int,
                            progress_callback: Optional[Callable] = None,
                            subtitle_lang: str = "id") -> Optional[Path]:
        """Burn subtitle ke video dengan fallback methods"""
        try:
            logger.info(f"🔥 Burning subtitle: {subtitle_path} into {video_path}")

            if not video_path.exists():
                logger.error(f"❌ Video not found: {video_path}")
                return None
            if not subtitle_path.exists():
                logger.error(f"❌ Subtitle not found: {subtitle_path}")
                return None

            video_size = video_path.stat().st_size
            sub_size   = subtitle_path.stat().st_size
            logger.info(f"📊 Video: {video_size} bytes | Subtitle: {sub_size} bytes")

            # Prepare subtitle (encoding fix + format convert)
            converted_sub = await self.prepare_subtitle(subtitle_path)
            if converted_sub != subtitle_path:
                logger.info(f"✅ Using converted subtitle: {converted_sub}")
                subtitle_path = converted_sub

            await self.verify_subtitle(subtitle_path)

            methods = [
                (self._burn_method_1, "Method 1: With styling"),
                (self._burn_method_2, "Method 2: Absolute path"),
                (self._burn_method_3, "Method 3: ASS filter"),
                (self._burn_method_4, "Method 4: Copy+embed"),
            ]

            for method_func, method_name in methods:
                logger.info(f"🎬 Trying {method_name}...")
                result = await method_func(video_path, subtitle_path, output_path, user_id, progress_callback)
                if result and result.exists() and result.stat().st_size > 0:
                    logger.info(f"✅ {method_name} succeeded! {result.stat().st_size} bytes")
                    return result
                logger.warning(f"⚠️ {method_name} failed")

            logger.error("❌ All subtitle burning methods failed")
            return None

        except Exception as e:
            logger.error(f"❌ burn_subtitle exception: {e}")
            import traceback; traceback.print_exc()
            return None

    # ---- Burn sub methods -------------------------------------------- #
    def _common_encode_flags(self) -> list:
        """Kembalikan flags encoding standar yang dipakai di semua burn method"""
        return [
            "-c:v", VIDEO_CODEC,
            "-profile:v", VIDEO_PROFILE,
            "-level:v", VIDEO_LEVEL,
            "-preset", VIDEO_PRESET,
            "-tune", VIDEO_TUNE,
            "-b:v", TARGET_VIDEO_BITRATE,
            "-maxrate", TARGET_VIDEO_MAXRATE,
            "-bufsize", TARGET_VIDEO_BUFSIZE,
            "-pix_fmt", PIX_FMT,
            "-color_primaries", COLOR_PRIMARIES,
            "-color_trc", COLOR_TRC,
            "-colorspace", COLORSPACE,
            "-color_range", COLOR_RANGE,
            "-x264-params", X264_PARAMS,
            "-c:a", AUDIO_CODEC,
            "-b:a", TARGET_AUDIO_BITRATE,
            "-ac", str(AUDIO_CHANNELS),
            "-ar", str(AUDIO_SAMPLE_RATE),
            "-movflags", MOVFLAGS,
            "-threads", str(FFMPEG_THREADS),
        ]

    async def _burn_method_1(self, video_path, subtitle_path, output_path, user_id, cb):
        """Method 1: subtitles filter dengan styling"""
        vf = (
            f"subtitles={subtitle_path.name}:"
            "force_style='FontName=Arial,FontSize=16,"
            "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
            "BorderStyle=3,Outline=1,Shadow=0,MarginV=20'"
        )
        cmd = (
            ["ffmpeg", "-y", "-i", str(video_path), "-vf", vf]
            + self._common_encode_flags()
            + [str(output_path)]
        )
        return await self._run_ffmpeg(cmd, video_path.parent, output_path, user_id, cb)

    async def _burn_method_2(self, video_path, subtitle_path, output_path, user_id, cb):
        """Method 2: subtitles filter, absolute path, no styling"""
        vf = f"subtitles={str(subtitle_path)}"
        cmd = (
            ["ffmpeg", "-y", "-i", str(video_path), "-vf", vf]
            + self._common_encode_flags()
            + [str(output_path)]
        )
        return await self._run_ffmpeg(cmd, video_path.parent, output_path, user_id, cb)

    async def _burn_method_3(self, video_path, subtitle_path, output_path, user_id, cb):
        """Method 3: ASS filter"""
        vf = f"ass={subtitle_path}"
        cmd = (
            ["ffmpeg", "-y", "-i", str(video_path), "-vf", vf]
            + self._common_encode_flags()
            + [str(output_path)]
        )
        return await self._run_ffmpeg(cmd, video_path.parent, output_path, user_id, cb)

    async def _burn_method_4(self, video_path, subtitle_path, output_path, user_id, cb):
        """Method 4: Copy codec + embed subtitle as mov_text track"""
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(subtitle_path),
            "-c:v", "copy",
            "-c:a", "copy",
            "-c:s", "mov_text",
            "-movflags", MOVFLAGS,
            "-threads", str(FFMPEG_THREADS),
            str(output_path)
        ]
        return await self._run_ffmpeg(cmd, video_path.parent, output_path, user_id, cb)

    def _run_ffmpeg_sync(self, cmd: list, label: str, output_path: Path) -> Optional[Path]:
        """Runner FFmpeg sinkron untuk dipakai di loop.run_in_executor"""
        try:
            logger.info(f"▶ [{label}] {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='ignore'
            )
            if result.returncode != 0:
                logger.error(f"FFmpeg {label} error (rc={result.returncode}): {result.stderr[:500]}")
                return None
            
            if output_path.exists() and output_path.stat().st_size > 0:
                return output_path
            return None
        except Exception as e:
            logger.error(f"_run_ffmpeg_sync {label} exception: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  FFmpeg runner                                                       #
    # ------------------------------------------------------------------ #
    async def _run_ffmpeg(self, cmd: list, cwd: Path, output_path: Path,
                          user_id: int,
                          progress_callback: Optional[Callable] = None) -> Optional[Path]:
        try:
            logger.info(f"▶ {' '.join(cmd)}")
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            if user_id and getattr(self, 'task_tracker', None):
                self.task_tracker.register_process(user_id, process)
            
            try:
                if progress_callback:
                    asyncio.create_task(self._monitor_progress(process, output_path, progress_callback))

                stdout, stderr = await process.communicate()
            finally:
                if user_id and getattr(self, 'task_tracker', None):
                    self.task_tracker.unregister_process(user_id, process)

            if process.returncode != 0:
                err = stderr.decode('utf-8', errors='ignore')[:500]
                logger.error(f"FFmpeg error (rc={process.returncode}): {err}")
                return None

            if output_path.exists() and output_path.stat().st_size > 0:
                return output_path
            return None

        except Exception as e:
            logger.error(f"_run_ffmpeg exception: {e}")
            return None

    async def _monitor_progress(self, process, output_path: Path, callback):
        last_size  = 0
        stall_count = 0
        while process.returncode is None:
            if output_path.exists():
                size = output_path.stat().st_size
                if size > last_size:
                    try: await callback(size)
                    except: pass
                    last_size   = size
                    stall_count = 0
                else:
                    stall_count += 1
                    if stall_count > 30:
                        logger.warning("⚠️ FFmpeg stalled, terminating...")
                        process.terminate()
                        break
            await asyncio.sleep(1)

    async def _get_duration(self, file_path: Path) -> Optional[float]:
        """Ambil durasi video dalam detik pakai ffprobe"""
        try:
            cmd = [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(file_path)
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            return float(stdout.decode().strip())
        except:
            return None

    # ------------------------------------------------------------------ #
    #  SUBTITLE PREP                                                       #
    # ------------------------------------------------------------------ #
    async def prepare_subtitle(self, subtitle_path: Path) -> Path:
        try:
            fixed_path = await self._fix_encoding(subtitle_path)
            if fixed_path.suffix.lower() == '.vtt':
                converted = await self._convert_vtt_to_srt(fixed_path)
                if converted:
                    fixed_path = converted
            ass_path = await self._convert_to_ass(fixed_path)
            return ass_path if ass_path else fixed_path
        except Exception as e:
            logger.error(f"prepare_subtitle error: {e}")
            return subtitle_path

    async def _fix_encoding(self, subtitle_path: Path) -> Path:
        try:
            with open(subtitle_path, 'rb') as f:
                raw = f.read()
            detected   = chardet.detect(raw)
            encoding   = detected.get('encoding', 'utf-8')
            confidence = detected.get('confidence', 0)
            logger.info(f"📝 Encoding: {encoding} (conf: {confidence:.2f})")

            if encoding.lower() != 'utf-8' or confidence < 0.8:
                text = raw.decode(encoding, errors='replace')
                if text.startswith('\ufeff'):
                    text = text[1:]
                fixed = subtitle_path.with_suffix('.utf8.srt')
                async with aiofiles.open(fixed, 'w', encoding='utf-8') as f:
                    await f.write(text)
                logger.info(f"✅ Encoding fixed → UTF-8: {fixed}")
                return fixed
            return subtitle_path
        except Exception as e:
            logger.error(f"_fix_encoding error: {e}")
            return subtitle_path

    async def _convert_vtt_to_srt(self, vtt_path: Path) -> Optional[Path]:
        try:
            srt_path = vtt_path.with_suffix('.srt')
            async with aiofiles.open(vtt_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = await f.read()
            lines = content.split('\n')
            if lines and 'WEBVTT' in lines[0]:
                lines = lines[1:]
            processed = [line.replace('.', ',') if '-->' in line else line for line in lines]
            async with aiofiles.open(srt_path, 'w', encoding='utf-8') as f:
                await f.write('\n'.join(processed))
            if srt_path.exists() and srt_path.stat().st_size > 0:
                logger.info(f"✅ VTT → SRT: {srt_path}")
                return srt_path
        except Exception as e:
            logger.error(f"_convert_vtt_to_srt error: {e}")
        return None

    async def _convert_to_ass(self, subtitle_path: Path) -> Optional[Path]:
        try:
            ass_path = subtitle_path.with_suffix('.ass')
            cmd = ["ffmpeg", "-y", "-i", str(subtitle_path), str(ass_path)]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            if ass_path.exists() and ass_path.stat().st_size > 0:
                logger.info(f"✅ Subtitle → ASS: {ass_path}")
                return ass_path
        except Exception as e:
            logger.error(f"_convert_to_ass error: {e}")
        return None

    async def verify_subtitle(self, subtitle_path: Path):
        try:
            async with aiofiles.open(subtitle_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = await f.read(1000)
            if   'WEBVTT'        in content: fmt = 'VTT'
            elif '-->'           in content: fmt = 'SRT'
            elif '[Script Info]' in content: fmt = 'ASS'
            else:                            fmt = 'Unknown'
            logger.info(f"📝 Subtitle format: {fmt}")
            logger.info(f"📝 Preview: {content[:200].replace(chr(10), ' ').strip()}")
        except Exception as e:
            logger.error(f"verify_subtitle error: {e}")

    # ------------------------------------------------------------------ #
    #  MEDIAINFO REPORT — Generate & upload ke Telegraph, return link      #
    # ------------------------------------------------------------------ #
    async def generate_mediainfo_report(self, video_path: Path) -> Optional[str]:
        """
        Generate laporan MediaInfo dari video menggunakan ffprobe,
        upload ke Telegraph sebagai halaman, dan kembalikan URL-nya.
        """
        try:
            filename = video_path.name
            if not video_path.exists():
                logger.warning(f"⚠️ MediaInfo: file tidak ada: {video_path}")
                return None

            # ── 1. Ambil info via ffprobe ────────────────────────────────────
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-show_format",
                str(video_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                logger.error("ffprobe gagal menganalisa video")
                return None

            data    = _json.loads(stdout.decode("utf-8", errors="ignore"))
            fmt     = data.get("format", {})
            streams = data.get("streams", [])

            # ── Helpers ──────────────────────────────────────────────────────
            def _dur(s):
                try:
                    s = float(s)
                    h, r = divmod(int(s), 3600)
                    m, sc = divmod(r, 60)
                    if h:   return f"{h}h {m:02d}m {sc:02d}s"
                    elif m: return f"{m} min {sc} s"
                    else:   return f"{sc} s"
                except Exception:
                    return str(s) if s else "N/A"

            def _size(b):
                try:
                    b = int(b)
                    if b >= 1024**3: return f"{b/1024**3:.2f} GB"
                    if b >= 1024**2: return f"{b/1024**2:.2f} MB"
                    if b >= 1024:    return f"{b/1024:.2f} KB"
                    return f"{b} B"
                except Exception:
                    return "N/A"

            def _br(v):
                try:    return f"{int(v)//1000} kb/s"
                except Exception: return "N/A"

            def _fps(v):
                try:
                    if "/" in str(v):
                        a, b = v.split("/")
                        return f"{int(a)/int(b):.3f} FPS"
                    return f"{float(v):.3f} FPS"
                except Exception:
                    return "N/A"

            def _level(v):
                try:    return f"{int(v)/10:.1f}"
                except Exception: return str(v)

            # ── 2. Susun node Telegraph ───────────────────────────────────────
            # API spec: content = JSON array of Node
            # Node = {"tag": str, "children": [str | Node], "attrs": {...}}
            # Tag yang didukung: a, aside, b, blockquote, br, code, em, figcaption,
            #                    figure, h3, h4, hr, i, iframe, img, li, ol, p,
            #                    pre, s, strong, u, ul, video

            def bold_line(key, val):
                return {
                    "tag": "p",
                    "children": [
                        {"tag": "b", "children": [str(key)]},
                        f" : {val}"
                    ]
                }

            def heading(text):
                return {"tag": "h4", "children": [str(text)]}

            def divider():
                return {"tag": "hr"}

            nodes = []

            # ── Header ───────────────────────────────────────────────────────
            filename     = video_path.name
            file_size_str = _size(fmt.get("size", ""))
            nodes.append({"tag": "h3", "children": [f"{filename}  [{file_size_str}]"]})
            nodes.append(divider())

            # ── General ──────────────────────────────────────────────────────
            nodes.append(heading("📦 General"))
            fmt_name   = fmt.get("format_long_name", fmt.get("format_name", "N/A"))
            duration   = _dur(fmt.get("duration", ""))
            overall_br = _br(fmt.get("bit_rate", ""))
            tags       = fmt.get("tags") or {}
            writing_app = tags.get("encoder") or tags.get("writing_application") or tags.get("Encoder") or ""

            nodes.append(bold_line("Format",           fmt_name))
            nodes.append(bold_line("File size",        file_size_str))
            nodes.append(bold_line("Duration",         duration))
            nodes.append(bold_line("Overall bit rate", overall_br))
            if writing_app:
                nodes.append(bold_line("Writing application", writing_app))

            # ── Per stream ───────────────────────────────────────────────────
            for st in streams:
                codec_type = st.get("codec_type", "").lower()
                codec_name = st.get("codec_name", "N/A").upper()
                sid        = st.get("index", "?")

                nodes.append(divider())

                if codec_type == "video":
                    profile  = st.get("profile", "")
                    level    = _level(st.get("level", ""))
                    prof_str = f"{profile}@L{level}" if profile and level else profile or ""
                    w, h     = st.get("width", ""), st.get("height", "")
                    fps      = _fps(st.get("r_frame_rate") or st.get("avg_frame_rate", ""))
                    vbr      = _br(st.get("bit_rate", ""))
                    pix      = st.get("pix_fmt", "N/A")
                    color_parts = [c for c in [
                        st.get("color_space",""), st.get("color_primaries",""),
                        st.get("color_transfer",""), st.get("color_range","")
                    ] if c]
                    enc = (st.get("tags") or {}).get("encoder","") \
                       or (st.get("tags") or {}).get("ENCODER","")

                    nodes.append(heading(f"🎬 Video  (ID:{sid})"))
                    nodes.append(bold_line("Codec", f"{codec_name} ({prof_str})" if prof_str else codec_name))
                    if w and h:
                        nodes.append(bold_line("Resolution", f"{w}×{h}"))
                    nodes.append(bold_line("Frame rate",   fps))
                    if vbr != "N/A":
                        nodes.append(bold_line("Bit rate", vbr))
                    nodes.append(bold_line("Pixel format", pix))
                    if color_parts:
                        nodes.append(bold_line("Color", " / ".join(color_parts)))
                    if enc:
                        nodes.append(bold_line("Encoder", enc[:120]))

                elif codec_type == "audio":
                    sr     = st.get("sample_rate", "")
                    chs    = st.get("channels", "")
                    layout = st.get("channel_layout", "")
                    abr    = _br(st.get("bit_rate", ""))
                    lang   = (st.get("tags") or {}).get("language", "")
                    sr_str = f"{int(sr)/1000:.1f} kHz" if sr else "N/A"
                    ch_str = f"{chs}ch ({layout})" if layout else (f"{chs}ch" if chs else "N/A")

                    nodes.append(heading(f"🔊 Audio  (ID:{sid})"))
                    nodes.append(bold_line("Codec",       codec_name))
                    nodes.append(bold_line("Sample rate", sr_str))
                    nodes.append(bold_line("Channels",    ch_str))
                    if abr != "N/A":
                        nodes.append(bold_line("Bit rate", abr))
                    if lang:
                        nodes.append(bold_line("Language", lang))

                elif codec_type == "subtitle":
                    lang      = (st.get("tags") or {}).get("language", "")
                    title     = (st.get("tags") or {}).get("title", "")
                    default_  = st.get("disposition", {}).get("default", 0)
                    forced_   = st.get("disposition", {}).get("forced", 0)

                    nodes.append(heading(f"💬 Subtitle  (ID:{sid})"))
                    nodes.append(bold_line("Codec",   codec_name))
                    if lang:  nodes.append(bold_line("Language", lang))
                    if title: nodes.append(bold_line("Title",    title))
                    nodes.append(bold_line("Default", "Yes ✅" if default_ else "No"))
                    nodes.append(bold_line("Forced",  "Yes"    if forced_  else "No"))

            nodes.append(divider())
            nodes.append({"tag": "p", "children": ["📊 Generated by ffprobe"]})

            # ── 3. Upload ke Telegraph ────────────────────────────────────────
            # PENTING: Telegraph /createPage harus pakai form-encoded (data=),
            # bukan json=. Field 'content' adalah JSON string, bukan dict.
            content_str = _json.dumps(nodes, ensure_ascii=False)
            page_title  = f"MediaInfo — {filename}"

            # Ambil token valid (auto-create jika belum ada)
            token = await self._get_telegraph_token()
            if not token:
                logger.error("❌ Tidak bisa mendapatkan Telegraph token")
                return None

            form_data = aiohttp.FormData()
            form_data.add_field("access_token",   token)
            form_data.add_field("title",           page_title)
            form_data.add_field("content",         content_str)
            form_data.add_field("return_content",  "false")

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.telegra.ph/createPage",
                    data=form_data
                ) as resp:
                    res_json = await resp.json()
                    if res_json.get("ok"):
                        url = res_json["result"]["url"]
                        logger.info(f"✅ MediaInfo Telegraph page created: {url}")
                        return url
                    else:
                        logger.error(f"Telegraph API error: {res_json}")
                        return None
        except Exception as e:
            logger.error(f"generate_mediainfo_report failed: {e}")
            return None

    async def get_detailed_mediainfo_string(self, video_path: Path) -> str:
        """
        Get media info as a formatted string for Telegram message.
        """
        import json as _json
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-show_format",
                str(video_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            data = _json.loads(stdout.decode("utf-8", errors="ignore"))
            fmt = data.get("format", {})
            streams = data.get("streams", [])
            
            video = next((s for s in streams if s.get("codec_type") == "video"), {})
            audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
            subs = [s for s in streams if s.get("codec_type") == "subtitle"]
            
            def _format_size(b):
                b = int(b)
                if b >= 1024**3: return f"{b/1024**3:.2f} GB"
                return f"{b/1024**2:.2f} MB"

            def _format_dur(s):
                s = float(s)
                m, sc = divmod(int(s), 60)
                h, m = divmod(m, 60)
                return f"{h:02d}:{m:02d}:{sc:02d}"

            info = (
                f"📊 <b>MEDIA INFO</b>\n\n"
                f"📦 <b>File Size:</b> {_format_size(fmt.get('size', 0))}\n"
                f"⏱ <b>Duration:</b> {_format_dur(fmt.get('duration', 0))}\n"
                f"📺 <b>Resolution:</b> {video.get('width', 'N/A')}x{video.get('height', 'N/A')}\n"
                f"🎞 <b>Codec:</b> {video.get('codec_name', 'N/A').upper()}\n"
                f"⚡️ <b>Bitrate:</b> {int(fmt.get('bit_rate', 0))//1000} kbps\n"
            )
            
            if subs:
                langs = [s.get("tags", {}).get("language", "und") for s in subs]
                info += f"💬 <b>Subtitle Language:</b> {', '.join(langs).upper()}\n"
            else:
                info += f"💬 <b>Subtitle:</b> None\n"
                
            return info
        except Exception as e:
            logger.error(f"get_detailed_mediainfo_string error: {e}")
            return f"❌ Error getting media info: {str(e)}"

    # ------------------------------------------------------------------ #
    #  TELEGRAPH TOKEN – Auto-create & cache                               #
    # ------------------------------------------------------------------ #
    _telegraph_token: Optional[str] = None
    _TELEGRAPH_TOKEN_FILE = Path("telegraph_token.txt")

    async def _get_telegraph_token(self) -> Optional[str]:
        """
        Kembalikan Telegraph access_token yang valid.
        Urutan prioritas:
          1. Cache in-memory  (tidak perlu I/O)
          2. File telegraph_token.txt  (persist antar restart)
          3. Buat akun baru via /createAccount → simpan ke file
        """
        # 1. In-memory cache
        if VideoProcessor._telegraph_token:
            return VideoProcessor._telegraph_token

        # 2. Baca dari file
        token_file = VideoProcessor._TELEGRAPH_TOKEN_FILE
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                VideoProcessor._telegraph_token = token
                logger.info(f"✅ Telegraph token loaded: {token_file}")
                return token

        # 3. Buat akun baru
        logger.info("⚙️ Membuat Telegraph account baru...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.telegra.ph/createAccount",
                    params={
                        "short_name":  "MediaInfoBot",
                        "author_name": "MediaInfo Bot",
                    },
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    data = await resp.json(content_type=None)

            if data.get("ok"):
                token = data["result"]["access_token"]
                token_file.write_text(token, encoding="utf-8")
                VideoProcessor._telegraph_token = token
                logger.info(f"✅ Telegraph token baru disimpan → {token_file}")
                return token
            else:
                logger.error(f"❌ createAccount gagal: {data.get('error', data)}")
                return None

        except Exception as e:
            logger.error(f"❌ _get_telegraph_token error: {e}")
            return None
            return None