import asyncio
import subprocess
import tempfile
import shutil
import json
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any, Callable, List, Tuple
from urllib.parse import urlparse, urljoin, parse_qs
from datetime import datetime

import aiohttp
import aiofiles

from utils import logger, FileCleanup, format_size, SubtitleDetector
from config import (
    DOWNLOAD_TIMEOUT, PROCESSING_TIMEOUT, MAX_SEGMENT_RETRIES,
    SEGMENT_CONCURRENCY, TARGET_FILE_SIZE_MB, TARGET_VIDEO_BITRATE,
    TARGET_VIDEO_MAXRATE, TARGET_VIDEO_BUFSIZE,
    TARGET_AUDIO_BITRATE, AUDIO_CHANNELS, AUDIO_SAMPLE_RATE,
    SEGMENT_TIMEOUT, PLAYLIST_REFRESH_INTERVAL,
    ENABLE_AUTO_COMPRESS, MIN_BITRATE, MAX_BITRATE, COMPRESSION_ATTEMPTS,
    VIDEO_CODEC, VIDEO_PROFILE, VIDEO_LEVEL, VIDEO_PRESET, VIDEO_TUNE,
    PIX_FMT, COLOR_PRIMARIES, COLOR_TRC, COLORSPACE, COLOR_RANGE,
    X264_PARAMS, AUDIO_CODEC, MOVFLAGS, FFMPEG_THREADS, DOWNLOAD_PROXY
)

try:
    from shortmax import ShortmaxDecryptor
except ImportError:
    ShortmaxDecryptor = None

class HLSStreamInfo:
    """Informasi lengkap tentang HLS stream - UPDATED with audio & subtitle support"""
    def __init__(self):
        self.url: str = ""
        self.is_master: bool = False
        self.video_playlist: Optional[str] = None
        self.audio_playlist: Optional[str] = None
        self.subtitle_tracks: List[Dict] = []
        self.video_segments: List[str] = []
        self.audio_segments: List[str] = []
        self.duration: float = 0.0
        self.has_audio: bool = False
        self.has_subtitle: bool = False
        self.bandwidth: int = 0
        self.resolution: str = ""
        self.needs_merge: bool = False
        self.headers: Dict[str, str] = {}
        self.token_url: str = ""
        self.master_playlist_content: str = ""  # Simpan master playlist untuk referensi
        # Untuk audio tracks
        self.audio_tracks: List[Dict] = []
        self.selected_audio: Optional[Dict] = None
        # Untuk subtitle tracks
        self.selected_subtitle: Optional[Dict] = None
        # Semua video variants dari master playlist (untuk pilihan kualitas)
        # Setiap item: {"label": "1080p", "url": "...", "bandwidth": 12345, "resolution": "1920x1080"}
        self.variants: List[Dict] = []

from task_tracker import TaskTracker

class OptimizedHLSDownloader:
    """
    HLS Downloader dengan FIX DURASI VIDEO:
    - Mencegah video jadi slow motion (2 menit → 10 menit)
    - Deteksi otomatis video + audio terpisah
    - Parallel segment download
    - Token refresh otomatis
    - Kompresi otomatis < 50MB
    - Merge stream otomatis
    - Subtitle handling
    """
    
    def __init__(self, task_tracker: Optional[TaskTracker] = None):
        self._session: Optional[aiohttp.ClientSession] = None
        self._temp_dirs: List[Path] = []
        self._playlist_cache: Dict[str, Tuple[str, float]] = {}  # url -> (content, timestamp)
        self.task_tracker = task_tracker
        
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session dengan headers yang sesuai"""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, ssl=False)
            timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
            self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self._session

    def _build_headers(self, url: str, referer: str = None) -> Dict[str, str]:
        """Build headers yang tepat berdasarkan URL - ENHANCED for Rishort"""
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        
        # Base headers
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, video/mp4, */*",
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
        }
        
        # Rishort / GoodShort specific
        if "rishort.com" in host or "goodshort" in host or "workers.dev" in host:
            headers.update({
                "Origin": "https://new.rishort.com",
                "Referer": referer or "https://new.rishort.com/",
                "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Site": "cross-site",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            })
        
        return headers

    async def _fetch_with_retry(self, url: str, headers: Dict = None, 
                                max_retries: int = 5) -> Optional[str]:
        """Fetch URL dengan retry mechanism dan better error handling"""
        session = await self._get_session()
        headers = headers or self._build_headers(url)
        
        for attempt in range(max_retries):
            try:
                logger.info(f"🌐 Fetching (attempt {attempt+1}/{max_retries}): {url[:100]}...")
                
                async with session.get(
                    url, 
                    headers=headers, 
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=30),
                    proxy=DOWNLOAD_PROXY
                ) as resp:
                    
                    if resp.status == 200:
                        content = await resp.text()
                        logger.info(f"✅ Success ({len(content)} bytes)")
                        return content
                        
                    elif resp.status == 403:
                        logger.warning(f"🚫 HTTP 403 - Attempt {attempt+1}")
                        
                        # Coba dengan headers berbeda berdasarkan attempt
                        if attempt == 0:
                            headers.update({
                                "Accept": "*/*",
                                "Cache-Control": "no-cache",
                            })
                        elif attempt == 1:
                            # Coba tanpa referer
                            headers.pop("Referer", None)
                            headers.pop("Origin", None)
                        elif attempt == 2:
                            # Coba dengan user-agent mobile
                            headers["User-Agent"] = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
                        elif attempt == 3:
                            # Coba dengan tambahan headers
                            headers.update({
                                "X-Requested-With": "XMLHttpRequest",
                                "Sec-Fetch-Site": "same-origin",
                            })
                        
                        await asyncio.sleep(2 ** attempt)  # Exponential backoff
                        continue
                        
                    elif resp.status in [401, 404, 410]:
                        logger.error(f"❌ HTTP {resp.status} - URL expired/invalid")
                        return None
                        
                    else:
                        logger.error(f"❌ HTTP {resp.status}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return None
                        
            except asyncio.TimeoutError:
                logger.warning(f"⏱️ Timeout attempt {attempt+1}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None
                    
            except aiohttp.ClientError as e:
                logger.error(f"🔌 Client error: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None
                    
            except Exception as e:
                logger.error(f"💥 Error: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None
        
        return None

    async def analyze_stream(self, url: str) -> Optional[HLSStreamInfo]:
        """
        Analisis lengkap HLS stream dengan dukungan audio & subtitle
        """
        stream_info = HLSStreamInfo()
        stream_info.url = url
        stream_info.token_url = url
        
        try:
            # Special handling untuk Rishort HLS Proxy
            if "hls-proxy.rishort.workers.dev" in url or "hls/m3u8" in url or "hls/proxy" in url:
                logger.info("🎯 Detected Rishort HLS Proxy, using enhanced parser")
                
                # Fetch dengan headers lengkap
                headers = self._build_headers(url, referer="https://new.rishort.com/")
                content = await self._fetch_with_retry(url, headers=headers, max_retries=5)
                
                if not content:
                    logger.error("❌ Gagal fetch playlist")
                    return None
                
                # Cek apakah ini master playlist
                if "#EXT-X-STREAM-INF" in content:
                    stream_info.is_master = True
                    await self._parse_master_playlist_enhanced(content, url, stream_info)
                else:
                    # Media playlist langsung (kemungkinan hanya video)
                    logger.info("📹 Direct media playlist detected")
                    await self._parse_media_playlist(content, url, stream_info)
                
                return stream_info
            
            # Regular HLS processing
            else:
                content = await self._fetch_with_retry(url)
                if not content:
                    return None
                
                if "#EXT-X-STREAM-INF" in content:
                    stream_info.is_master = True
                    await self._parse_master_playlist_enhanced(content, url, stream_info)
                else:
                    await self._parse_media_playlist(content, url, stream_info)
                
                return stream_info
                
        except Exception as e:
            logger.error(f"Error analyzing stream: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def _parse_master_playlist_enhanced(self, content: str, base_url: str, 
                                              stream_info: HLSStreamInfo):
        """
        Parse master playlist dengan deteksi lengkap:
        - Video variants
        - Audio tracks terpisah
        - Subtitle tracks
        """
        lines = content.splitlines()
        stream_info.master_playlist_content = content
        
        # Koleksi untuk semua variants
        variants = []
        audio_tracks = []
        subtitle_tracks = []
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Deteksi video variants
            if line.startswith("#EXT-X-STREAM-INF"):
                attrs = self._parse_attributes(line)
                bandwidth = int(attrs.get("BANDWIDTH", 0))
                resolution = attrs.get("RESOLUTION", "")
                codecs = attrs.get("CODECS", "")
                
                # Cari audio group
                audio_group = attrs.get("AUDIO", "")
                
                # Cari subtitle group
                subtitle_group = attrs.get("SUBTITLES", "")
                
                # URL video ada di baris berikutnya
                if i + 1 < len(lines) and not lines[i+1].startswith("#"):
                    uri = lines[i+1].strip()
                    video_url = urljoin(base_url, uri)
                    
                    variants.append({
                        "url": video_url,
                        "bandwidth": bandwidth,
                        "resolution": resolution,
                        "codecs": codecs,
                        "audio_group": audio_group,
                        "subtitle_group": subtitle_group
                    })
                    logger.info(f"📹 Found video variant: {bandwidth}bps, {resolution}")
                    i += 1
            
            # Deteksi audio tracks
            elif line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
                attrs = self._parse_attributes(line)
                group_id = attrs.get("GROUP-ID", "")
                name = attrs.get("NAME", "")
                language = attrs.get("LANGUAGE", "")
                uri = attrs.get("URI", "")
                default = attrs.get("DEFAULT", "NO") == "YES"
                autoselect = attrs.get("AUTOSELECT", "NO") == "YES"
                
                if uri:
                    audio_url = urljoin(base_url, uri)
                    audio_tracks.append({
                        "url": audio_url,
                        "group_id": group_id,
                        "name": name,
                        "language": language,
                        "default": default,
                        "autoselect": autoselect,
                        "uri": uri
                    })
                    logger.info(f"🎵 Found audio track: {name} ({language})")
            
            # Deteksi subtitle tracks
            elif line.startswith("#EXT-X-MEDIA:TYPE=SUBTITLES"):
                attrs = self._parse_attributes(line)
                group_id = attrs.get("GROUP-ID", "")
                name = attrs.get("NAME", "")
                language = attrs.get("LANGUAGE", "")
                uri = attrs.get("URI", "")
                default = attrs.get("DEFAULT", "NO") == "YES"
                
                if uri:
                    subtitle_url = urljoin(base_url, uri)
                    # Use enhanced SubtitleDetector for consistent identification
                    is_indonesian = SubtitleDetector.is_indonesian_subtitle({
                        "name": name, 
                        "language": language, 
                        "uri": uri,
                        "label": name
                    })
                    
                    subtitle_tracks.append({
                        "url": subtitle_url,
                        "group_id": group_id,
                        "name": name,
                        "language": language,
                        "default": default,
                        "is_indonesian": is_indonesian,
                        "uri": uri
                    })
                    if is_indonesian:
                        logger.info(f"[SUB-DETECTION] HLS Subtitle Track found: {name} ({language}) -> INDONESIAN")
                    else:
                        logger.info(f"📝 Found subtitle track: {name} ({language})")
            
            i += 1
        
        # Simpan semua tracks
        stream_info.audio_tracks = audio_tracks
        stream_info.subtitle_tracks = subtitle_tracks
        stream_info.has_audio = len(audio_tracks) > 0
        stream_info.has_subtitle = len(subtitle_tracks) > 0

        # Pilih video variant terbaik
        if variants:
            # Urutkan berdasarkan bandwidth
            variants.sort(key=lambda x: x["bandwidth"], reverse=True)

            # Simpan semua variants dengan label resolusi (untuk keyboard kualitas)
            seen_labels = set()
            stream_info.variants = []
            for v in variants:
                res = v.get("resolution", "")
                # Buat label: "1080p", "720p", dst — fallback ke bandwidth jika tidak ada resolusi
                if res:
                    height = res.split("x")[-1] if "x" in res else res
                    label = f"{height}p"
                else:
                    label = f"{v['bandwidth'] // 1000}kbps"
                # Hindari duplikat label
                base_label = label
                suffix = 1
                while label in seen_labels:
                    label = f"{base_label}_{suffix}"
                    suffix += 1
                seen_labels.add(label)
                stream_info.variants.append({
                    "label":      label,
                    "url":        v["url"],
                    "bandwidth":  v["bandwidth"],
                    "resolution": res,
                    "audio_group":    v.get("audio_group", ""),
                    "subtitle_group": v.get("subtitle_group", ""),
                })

            best_variant = variants[0]
            stream_info.bandwidth = best_variant["bandwidth"]
            stream_info.resolution = best_variant["resolution"]
            
            logger.info(f"📹 Selected best video: {best_variant['bandwidth']}bps, {best_variant['resolution']}")
            
            # Simpan informasi audio group yang sesuai
            if best_variant["audio_group"] and audio_tracks:
                # Cari audio track yang cocok dengan group
                matching_audio = [a for a in audio_tracks if a["group_id"] == best_variant["audio_group"]]
                if matching_audio:
                    # Pilih yang default atau pertama
                    default_audio = next((a for a in matching_audio if a["default"]), matching_audio[0])
                    stream_info.selected_audio = default_audio
                    logger.info(f"🔊 Selected audio: {default_audio['name']} ({default_audio['language']})")
            
            # Pilih subtitle (prioritas Indonesian via SubtitleDetector)
            if best_variant["subtitle_group"] and subtitle_tracks:
                matching_subs = [s for s in subtitle_tracks if s["group_id"] == best_variant["subtitle_group"]]
                if matching_subs:
                    # Use centralized best-match logic
                    best_sub = SubtitleDetector.find_indonesian_subtitle(matching_subs)
                    if best_sub:
                        stream_info.selected_subtitle = best_sub
                        logger.info(f"[SUB-DETECTION] Selected best Indonesian subtitle: {best_sub['name']}")
                    else:
                        # Fallback: Ambil default atau pertama
                        default_sub = next((s for s in matching_subs if s["default"]), matching_subs[0])
                        stream_info.selected_subtitle = default_sub
                        logger.info(f"📝 Selected default/fallback subtitle: {default_sub['name']}")
            
            # Fetch media playlist untuk video
            media_content = await self._fetch_with_retry(best_variant["url"])
            if media_content:
                await self._parse_media_playlist(media_content, best_variant["url"], stream_info)
            
            # Fetch audio playlist jika ada
            if stream_info.selected_audio:
                audio_content = await self._fetch_with_retry(stream_info.selected_audio["url"])
                if audio_content:
                    await self._parse_audio_playlist(audio_content, stream_info.selected_audio["url"], stream_info)
        else:
            logger.warning("No video variants found in master playlist")

    async def _parse_media_playlist(self, content: str, base_url: str, 
                                     stream_info: HLSStreamInfo):
        """Parse media playlist, ekstrak segmen video"""
        lines = content.splitlines()
        
        # Reset video segments
        stream_info.video_segments = []
        
        for line in lines:
            if line.startswith("#EXTINF"):
                # Next line is segment URL
                pass
            elif not line.startswith("#") and line.strip():
                # Ini adalah URL segmen
                segment_url = urljoin(base_url, line.strip())
                stream_info.video_segments.append(segment_url)
        
        logger.info(f"📹 Found {len(stream_info.video_segments)} video segments")

    async def _parse_audio_playlist(self, content: str, base_url: str, stream_info: HLSStreamInfo):
        """Parse audio media playlist, ekstrak segmen audio"""
        lines = content.splitlines()
        
        # Reset audio segments
        stream_info.audio_segments = []
        
        for line in lines:
            if line.startswith("#EXTINF"):
                # Next line is segment URL
                pass
            elif not line.startswith("#") and line.strip():
                # Ini adalah URL segmen audio
                segment_url = urljoin(base_url, line.strip())
                stream_info.audio_segments.append(segment_url)
        
        logger.info(f"🎵 Found {len(stream_info.audio_segments)} audio segments")
        stream_info.has_audio = len(stream_info.audio_segments) > 0

    def _parse_attributes(self, line: str) -> Dict[str, str]:
        """Parse EXT-X-* attribute lines"""
        attrs = {}
        # Extract attributes inside quotes
        pattern = r'([A-Za-z0-9_-]+)="([^"]*)"'
        for match in re.finditer(pattern, line):
            attrs[match.group(1)] = match.group(2)
        return attrs

    def _parse_subtitle_tracks(self, content: str, base_url: str) -> List[Dict]:
        """Parse subtitle tracks dari playlist"""
        tracks = []
        pattern = r'#EXT-X-MEDIA:TYPE=SUBTITLES.*?URI="([^"]+)".*?LANGUAGE="([^"]*)"'
        
        for match in re.finditer(pattern, content, re.DOTALL):
            uri = urljoin(base_url, match.group(1))
            lang = match.group(2)
            
            # Deteksi subtitle Indonesia using detector
            is_indonesian = SubtitleDetector.is_indonesian_subtitle({"language": lang, "uri": uri})
            
            tracks.append({
                "url": uri,
                "language": lang,
                "is_indonesian": is_indonesian
            })
            if is_indonesian:
                logger.info(f"[SUB-DETECTION] Parsed subtitle track: {lang} -> INDONESIAN")
        
        return tracks

    async def _calculate_duration(self, stream_info: HLSStreamInfo):
        """Hitung durasi total dari segmen"""
        if stream_info.video_segments:
            # Download segmen pertama untuk sample
            sample_url = stream_info.video_segments[0]
            headers = self._build_headers(sample_url)
            session = await self._get_session()
            
            try:
                async with session.head(sample_url, headers=headers) as resp:
                    if resp.content_length:
                        duration_per_seg = resp.content_length / (1000 * 1000)  # detik per MB
                        stream_info.duration = duration_per_seg * len(stream_info.video_segments)
            except:
                pass

    async def _detect_segment_format(self, segment_path: Path) -> str:
        """
        Deteksi format segment berdasarkan ekstensi dan magic bytes.
        Returns: 'fmp4' jika fragmented MP4, 'ts' jika MPEG-TS, 'unknown' lainnya.
        """
        try:
            # Cek ekstensi dulu (lebih cepat)
            ext = segment_path.suffix.lower()
            if ext in ('.m4s', '.m4v', '.m4a'):
                return 'fmp4'
            if ext == '.ts':
                return 'ts'

            # Fallback: magic bytes
            with open(segment_path, 'rb') as f:
                header = f.read(12)
            if len(header) >= 8:
                box_type = header[4:8]
                if box_type in (b'ftyp', b'styp', b'moof', b'free', b'pnot', b'mdat', b'moov'):
                    return 'fmp4'
                if header[0:1] == b'\x47':
                    return 'ts'
            return 'unknown'
        except Exception:
            return 'unknown'

    async def _merge_segments(self, segments: List[Path], output_path: Path) -> bool:
        """Merge TS segments using binary concat"""
        try:
            with open(output_path, 'wb') as out_f:
                for seg in sorted(segments, key=lambda p: p.name):
                    with open(seg, 'rb') as in_f:
                        out_f.write(in_f.read())
            return output_path.exists() and output_path.stat().st_size > 0
        except Exception as e:
            logger.error(f"Binary merge failed: {e}")
            return False

    async def _merge_raw_ts(self, video_ts: Path, audio_ts: Optional[Path], output_path: Path) -> bool:
        """Merge raw TS video and audio files if needed"""
        if not audio_ts:
            try:
                shutil.copy(str(video_ts), str(output_path))
                return True
            except:
                return False
        
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_ts),
            "-i", str(audio_ts),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c", "copy",
            "-f", "mpegts",
            str(output_path)
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
            return proc.returncode == 0 and output_path.exists()
        except:
            return False


    async def _merge_fmp4_segments(self, segments: List[Path], output_path: Path,
                                    audio_segments: Optional[List[Path]],
                                    progress_callback: Optional[Callable],
                                    user_id: Optional[int] = None,
                                    output_format: str = "mp4",
                                    subtitle_file: Optional[Path] = None,
                                    burn_subtitle: bool = False) -> Optional[Path]:
        """
        Merge fragmented MP4 (fMP4) segments dengan 4 strategi fallback:
        1. ffmpeg concat demuxer (works for many fMP4 streams)
        2. ffmpeg concat protocol (cat-like, then remux)
        3. Binary concat + ffmpeg -c copy re-mux
        4. Binary concat + full re-encode libx264
        """
        temp_dir = output_path.parent
        sorted_video = sorted(segments, key=lambda p: p.name)
        has_sep_audio = bool(audio_segments and len(audio_segments) > 0)
        sorted_audio = sorted(audio_segments, key=lambda p: p.name) if has_sep_audio else []

        # ── Tulis concat list ─────────────────────────────────────────────────
        concat_v = temp_dir / "fmp4_video_concat.txt"
        async with aiofiles.open(concat_v, 'w') as f:
            for seg in sorted_video:
                await f.write(f"file '{seg.absolute()}'\n")

        concat_a: Optional[Path] = None
        if has_sep_audio:
            concat_a = temp_dir / "fmp4_audio_concat.txt"
            async with aiofiles.open(concat_a, 'w') as f:
                for seg in sorted_audio:
                    await f.write(f"file '{seg.absolute()}'\n")

        async def _run(cmd, label) -> bool:
            try:
                logger.info(f"[fMP4] Trying {label}...")
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                if user_id and getattr(self, 'task_tracker', None):
                    self.task_tracker.register_process(user_id, proc)
                try:
                    if progress_callback:
                        asyncio.create_task(self._monitor_ffmpeg_progress(
                            proc, output_path, progress_callback
                        ))
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROCESSING_TIMEOUT)
                finally:
                    if user_id and getattr(self, 'task_tracker', None):
                        self.task_tracker.unregister_process(user_id, proc)
                if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                    size_mb = output_path.stat().st_size / (1024 * 1024)
                    logger.info(f"✅ [fMP4] {label} OK: {size_mb:.2f} MB")
                    return True
                err = stderr.decode('utf-8', errors='ignore')[:300] if stderr else ""
                logger.warning(f"[fMP4] {label} failed: {err}")
            except asyncio.TimeoutError:
                logger.warning(f"[fMP4] {label} timeout")
            except Exception as e:
                logger.warning(f"[fMP4] {label} error: {e}")
            # Hapus output rusak sebelum coba berikutnya
            if output_path.exists():
                output_path.unlink()
            return False

        # Helper: movflags hanya untuk MP4
        movflags_args = ["-movflags", MOVFLAGS] if output_format.lower() == "mp4" else []
        result: Optional[Path] = None

        # ── Strategi 1: ffmpeg concat demuxer + copy ─────────────────────────
        if has_sep_audio and concat_a:
            cmd1 = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_v),
                "-f", "concat", "-safe", "0", "-i", str(concat_a),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c", "copy"
            ] + movflags_args + [str(output_path)]
        else:
            cmd1 = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", str(concat_v),
                "-c", "copy"
            ] + movflags_args + [str(output_path)]
        
        if await _run(cmd1, "concat demuxer + copy"):
            result = output_path

        # ── Strategi 2: concat demuxer + re-encode ────────────────────────────
        if not result:
            if has_sep_audio and concat_a:
                cmd2 = [
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0", "-i", str(concat_v),
                    "-f", "concat", "-safe", "0", "-i", str(concat_a),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
                    "-vf", "setpts=PTS-STARTPTS",
                    "-c:a", AUDIO_CODEC, "-b:a", TARGET_AUDIO_BITRATE, "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_SAMPLE_RATE),
                ] + movflags_args + [str(output_path)]
            else:
                cmd2 = [
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0", "-i", str(concat_v),
                    "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
                    "-vf", "setpts=PTS-STARTPTS",
                    "-c:a", AUDIO_CODEC, "-b:a", TARGET_AUDIO_BITRATE, "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_SAMPLE_RATE),
                ] + movflags_args + [str(output_path)]
            if await _run(cmd2, "concat demuxer + re-encode"):
                result = output_path

        # ── Strategi 3: Binary concat → ffmpeg copy ───────────────────────────
        if not result:
            raw_video = temp_dir / "raw_fmp4_video.mp4"
            raw_audio: Optional[Path] = None
            bin_has_audio = False
            try:
                with open(raw_video, 'wb') as out_f:
                    for seg in sorted_video:
                        with open(seg, 'rb') as in_f:
                            out_f.write(in_f.read())
                logger.info(f"[fMP4] Binary concat video: {raw_video.stat().st_size} bytes")
                
                if has_sep_audio:
                    raw_audio = temp_dir / "raw_fmp4_audio.mp4"
                    try:
                        with open(raw_audio, 'wb') as out_f:
                            for seg in sorted_audio:
                                with open(seg, 'rb') as in_f:
                                    out_f.write(in_f.read())
                        logger.info(f"[fMP4] Binary concat audio: {raw_audio.stat().st_size} bytes")
                        bin_has_audio = raw_audio.exists() and raw_audio.stat().st_size > 0
                    except Exception as e:
                        logger.warning(f"[fMP4] Binary concat audio failed: {e}")
                        raw_audio = None
                
                if bin_has_audio:
                    cmd3 = [
                        "ffmpeg", "-y",
                        "-i", str(raw_video), "-i", str(raw_audio),
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c", "copy"
                    ] + movflags_args + [str(output_path)]
                else:
                    cmd3 = [
                        "ffmpeg", "-y", "-i", str(raw_video),
                        "-c", "copy"
                    ] + movflags_args + [str(output_path)]
                
                if await _run(cmd3, "binary concat + copy"):
                    result = output_path
            except Exception as e:
                logger.error(f"[fMP4] Binary concat strategy failed: {e}")

        # ── Strategi 4: Binary concat → full re-encode ────────────────────────
        if not result and raw_video.exists():
            bin_has_audio = raw_audio and raw_audio.exists() and raw_audio.stat().st_size > 0
            if bin_has_audio:
                cmd4 = [
                    "ffmpeg", "-y",
                    "-i", str(raw_video), "-i", str(raw_audio),
                    "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
                    "-vf", "setpts=PTS-STARTPTS",
                    "-c:a", AUDIO_CODEC, "-b:a", TARGET_AUDIO_BITRATE, "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_SAMPLE_RATE),
                ] + movflags_args + [str(output_path)]
            else:
                cmd4 = [
                    "ffmpeg", "-y", "-i", str(raw_video),
                    "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
                    "-vf", "setpts=PTS-STARTPTS",
                    "-c:a", AUDIO_CODEC, "-b:a", TARGET_AUDIO_BITRATE, "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_SAMPLE_RATE),
                ] + movflags_args + [str(output_path)]
            if await _run(cmd4, "binary concat + re-encode"):
                result = output_path

        # ── Post-processing: Subtitles ────────────────────────────────────────
        if result and subtitle_file and subtitle_file.exists():
            final_output = temp_dir / f"fmp4_final_with_sub.{output_format.lower()}"
            success = False
            
            if burn_subtitle:
                logger.info("[fMP4] Burning subtitles into video...")
                try:
                    success = await self._burn_subtitle_to_video(
                        result, subtitle_file, final_output, progress_callback
                    )
                except Exception as sub_err:
                    logger.error(f"Burn subtitle error: {sub_err}")
            else:
                logger.info("[fMP4] Embedding subtitles (softsub)...")
                try:
                    success = await self._embed_subtitle(
                        result, subtitle_file, final_output, progress_callback
                    )
                except Exception as sub_err:
                    logger.error(f"Embed subtitle error: {sub_err}")
            
            if success and final_output.exists() and final_output.stat().st_size > 0:
                if output_path.exists():
                    try: output_path.unlink()
                    except: pass
                shutil.move(str(final_output), str(output_path))
                logger.info("[fMP4] Subtitle processing completed successfully")
                result = output_path
            else:
                logger.warning("[fMP4] Subtitle processing failed, using original video")

        if not result:
            logger.error("[fMP4] All 4 merge strategies failed")
            return None

        return result

    async def _embed_subtitle(self, video_path: Path, subtitle_path: Path, 
                              output_path: Path, progress_callback: Optional[Callable]) -> bool:
        """Embed subtitle track into video file (Softsub)"""
        try:
            is_mp4 = output_path.suffix.lower() == ".mp4"
            sub_codec = "mov_text" if is_mp4 else "copy"
            
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-i", str(subtitle_path),
                "-map", "0", "-map", "1",
                "-c", "copy", f"-c:s", sub_codec,
                "-disposition:s:0", "default",
                str(output_path)
            ]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROCESSING_TIMEOUT)
            
            if proc.returncode == 0 and output_path.exists():
                return True
            
            err = stderr.decode('utf-8', errors='ignore')
            logger.error(f"Embed subtitle failed: {err}")
            return False
        except Exception as e:
            logger.error(f"Error embedding subtitle: {e}")
            return False


    async def apply_variant(self, stream_info: HLSStreamInfo, chosen_label: str) -> bool:
        """
        Terapkan variant yang dipilih user ke stream_info.
        Mengubah video_playlist ke URL variant yang dipilih,
        lalu re-fetch media playlist agar video_segments ter-update.
        Return True jika berhasil, False jika label tidak ditemukan.

        Matching resolusi secara numerik:
          chosen_label = "720p"  → target_height = 720
          Variant "1920x1080"  → height = 1080  (skip, > 720)
          Variant "1280x720"   → height = 720   (match ✓)
          Variant "854x480"    → height = 480   (fallback jika 720 tidak ada)
        """
        if not stream_info.variants:
            return False

        # ── 1. Coba exact label match dulu ─────────────────────────────────
        chosen = next((v for v in stream_info.variants if v["label"] == chosen_label), None)

        # ── 2. Jika tidak cocok, match secara numerik ──────────────────────
        if not chosen:
            import re
            # Ambil angka dari chosen_label, misal "720p" → 720
            match = re.search(r'(\d+)', chosen_label)
            target_height = int(match.group(1)) if match else 0

            if target_height > 0:
                # Bangun list (variant, height) untuk semua variant yang punya resolusi
                height_variants = []
                for v in stream_info.variants:
                    res = v.get("resolution", "")
                    if "x" in res:
                        try:
                            h = int(res.split("x")[-1])
                            height_variants.append((v, h))
                        except ValueError:
                            pass
                    else:
                        # Coba ambil height dari label
                        lm = re.search(r'(\d+)', v.get("label", ""))
                        if lm:
                            try:
                                h = int(lm.group(1))
                                height_variants.append((v, h))
                            except ValueError:
                                pass

                if height_variants:
                    # Cari variant dengan height <= target_height, pilih yang paling tinggi
                    candidates = [(v, h) for v, h in height_variants if h <= target_height]
                    if candidates:
                        # Ambil yang height-nya paling dekat dengan target (paling tinggi di bawah/sama target)
                        chosen = max(candidates, key=lambda x: x[1])[0]
                    else:
                        # Semua variant lebih besar dari target → ambil yang paling kecil
                        chosen = min(height_variants, key=lambda x: x[1])[0]

                    logger.info(f"[quality] Numeric match: target={target_height}p → chosen={chosen['label']}")

        # ── 3. Fallback ke variant pertama jika masih tidak ditemukan ──────
        if not chosen:
            chosen = stream_info.variants[0]
            logger.warning(f"[quality] No match found for '{chosen_label}', fallback to {chosen['label']}")

        stream_info.video_playlist = chosen["url"]
        stream_info.bandwidth      = chosen["bandwidth"]
        stream_info.resolution     = chosen["resolution"]
        logger.info(f"[quality] Variant diterapkan: {chosen['label']} — {chosen['url'][:80]}")

        # Re-fetch media playlist agar video_segments cocok dengan variant baru
        try:
            media_content = await self._fetch_with_retry(chosen["url"])
            if media_content:
                await self._parse_media_playlist(media_content, chosen["url"], stream_info)
                logger.info(f"[quality] Re-parsed segments: {len(stream_info.video_segments)} segments")
        except Exception as e:
            logger.warning(f"[quality] Gagal re-fetch playlist variant: {e}")

        return True

    async def download_stream(self, stream_info: HLSStreamInfo,
                             output_path: Path,
                             user_id: int,
                             progress_callback: Optional[Callable] = None,
                             burn_subtitle: bool = False,
                             subtitle_url: Optional[str] = None,
                             output_format: str = "mp4") -> Optional[Path]:
        """
        Download dan proses HLS stream.
        Mendukung MPEG-TS dan fragmented MP4 (fMP4) segments secara otomatis.
        Fix: audio_ts adalah List[Path], bukan Path — tidak bisa pakai .exists()
        """
        temp_dir = Path(tempfile.mkdtemp(prefix="hls_fix_duration_"))
        self._temp_dirs.append(temp_dir)

        try:
            # ── Download segments ─────────────────────────────────────────────
            if not stream_info.video_segments:
                logger.error("No video segments to download")
                return None

            logger.info(f"📥 Downloading {len(stream_info.video_segments)} video segments...")
            video_ts = await self._download_segments_parallel(
                segments=stream_info.video_segments,
                temp_dir=temp_dir / "video",
                label="video",
                progress_callback=progress_callback,
                user_id=user_id
            )
            if not video_ts:
                logger.error("Failed to download video segments")
                return None

            # Download audio segments
            audio_ts: Optional[List[Path]] = None
            if stream_info.audio_segments:
                logger.info(f"🎵 Downloading {len(stream_info.audio_segments)} audio segments...")
                audio_ts = await self._download_segments_parallel(
                    segments=stream_info.audio_segments,
                    temp_dir=temp_dir / "audio",
                    label="audio",
                    progress_callback=None,
                    user_id=user_id
                )
                if not audio_ts:
                    logger.warning("Audio segments gagal didownload, lanjut tanpa audio terpisah")
                    audio_ts = None

            # Download subtitle jika diminta
            subtitle_file = None
            if burn_subtitle or subtitle_url:
                sub_url = subtitle_url
                if not sub_url and stream_info.selected_subtitle:
                    sub_url = stream_info.selected_subtitle.get("url")
                if sub_url:
                    logger.info(f"📝 Downloading subtitle for mode {'burn' if burn_subtitle else 'embed'}...")
                    subtitle_file = await self.download_subtitle(sub_url, temp_dir / "subtitle.vtt")

            # ── Deteksi format segment ────────────────────────────────────────
            logger.info("🎬 Merging segments with DURATION FIX...")
            segment_fmt = await self._detect_segment_format(video_ts[0])
            logger.info(f"📦 Segment format detected: {segment_fmt}")

            # Gunakan output_format yang direquest user
            temp_output = temp_dir / f"output_fixed.{output_format.lower()}"

            if segment_fmt == 'fmp4':
                # ── fMP4 path: binary concat + re-encode ──────────────────────
                logger.info("🔧 Using fMP4 merge path...")
                # Update: _merge_fmp4_segments now handles subtitles if provided
                result = await self._merge_fmp4_segments(
                    video_ts, temp_output, audio_ts, progress_callback, 
                    user_id=user_id, output_format=output_format,
                    subtitle_file=subtitle_file, burn_subtitle=burn_subtitle
                )
                if not result:
                    logger.error("fMP4 merge failed completely")
                    return None

            else:
                # ── MPEG-TS path: concat logic ───────────────────────────────
                logger.info("🔧 Using TS merge path...")
                
                # Check if we have subtitles to merge/burn
                if subtitle_file and subtitle_file.exists():
                    if audio_ts and len(audio_ts) > 0:
                        # Case: Video + Audio + Subtitle
                        # We temporarily merge video/audio first OR do it all in one ffmpeg call
                        # Let's use the helper _merge_video_audio_subtitle which is more robust
                        
                        # First, we need a single video file and single audio file from segments
                        raw_v = temp_dir / "temp_video.ts"
                        raw_a = temp_dir / "temp_audio.ts"
                        
                        v_ok = await self._merge_segments(video_ts, raw_v)
                        a_ok = await self._merge_segments(audio_ts, raw_a)
                        
                        if v_ok and a_ok:
                            success = await self._merge_video_audio_subtitle(
                                raw_v, raw_a, subtitle_file, temp_output, progress_callback
                            )
                            if not success:
                                logger.warning("Triple merge failed, falling back to video+audio only")
                            else:
                                result = temp_output
                        
                        if not temp_output.exists() or temp_output.stat().st_size == 0:
                            # Fallback to standard merge
                            audio_concat = temp_dir / "audio_concat.txt"
                            async with aiofiles.open(audio_concat, 'w') as f:
                                for seg in sorted(audio_ts, key=lambda p: p.name):
                                    await f.write(f"file '{seg.absolute()}'\n")
                            
                            concat_file = temp_dir / "concat.txt"
                            async with aiofiles.open(concat_file, 'w') as f:
                                for seg in sorted(video_ts, key=lambda p: p.name):
                                    await f.write(f"file '{seg.absolute()}'\n")
                                    
                            movflags_ts = ["-movflags", MOVFLAGS] if output_format.lower() == "mp4" else []
                            cmd = [
                                "ffmpeg", "-y",
                                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                                "-f", "concat", "-safe", "0", "-i", str(audio_concat),
                                "-c", "copy"
                            ] + movflags_ts + [str(temp_output)]
                            proc = await asyncio.create_subprocess_exec(*cmd)
                            await proc.communicate()
                    else:
                        # Case: Video + Subtitle (no separate audio)
                        raw_v = temp_dir / "temp_video.ts"
                        if await self._merge_segments(video_ts, raw_v):
                            success = await self._burn_subtitle_to_video(
                                raw_v, subtitle_file, temp_output, progress_callback
                            )
                            if not success:
                                # Fallback to simple conversion
                                await self._convert_to_mp4(raw_v, temp_output, progress_callback)
                else:
                    # Original TS logic (no subtitle)
                    concat_file = temp_dir / "concat.txt"
                    async with aiofiles.open(concat_file, 'w') as f:
                        for seg in sorted(video_ts, key=lambda p: p.name):
                            await f.write(f"file '{seg.absolute()}'\n")

                    if audio_ts and len(audio_ts) > 0:
                        audio_concat = temp_dir / "audio_concat.txt"
                        async with aiofiles.open(audio_concat, 'w') as f:
                            for seg in sorted(audio_ts, key=lambda p: p.name):
                                await f.write(f"file '{seg.absolute()}'\n")

                        movflags_ts = ["-movflags", MOVFLAGS] if output_format.lower() == "mp4" else []
                        cmd_b = [
                            "ffmpeg", "-y",
                            "-f", "concat", "-safe", "0", "-i", str(concat_file),
                            "-f", "concat", "-safe", "0", "-i", str(audio_concat),
                            "-vf", "setpts=PTS-STARTPTS", "-af", "aresample=async=1",
                            "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
                            "-c:a", AUDIO_CODEC, "-b:a", TARGET_AUDIO_BITRATE, "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_SAMPLE_RATE),
                            "-map", "0:v:0", "-map", "1:a:0",
                        ] + movflags_ts + [str(temp_output)]
                        proc = await asyncio.create_subprocess_exec(
                            *cmd_b, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )
                        if user_id and getattr(self, 'task_tracker', None):
                            self.task_tracker.register_process(user_id, proc)
                        try:
                            if progress_callback:
                                asyncio.create_task(self._monitor_ffmpeg_progress(proc, temp_output, progress_callback))
                            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROCESSING_TIMEOUT)
                        finally:
                            if user_id and getattr(self, 'task_tracker', None):
                                self.task_tracker.unregister_process(user_id, proc)

                    if not (audio_ts and len(audio_ts) > 0) or not temp_output.exists() or temp_output.stat().st_size == 0:
                        movflags_ts2 = ["-movflags", MOVFLAGS] if output_format.lower() == "mp4" else []
                        cmd_a = [
                            "ffmpeg", "-y",
                            "-f", "concat", "-safe", "0", "-i", str(concat_file),
                            "-vf", "setpts=PTS-STARTPTS", "-af", "aresample=async=1",
                            "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
                            "-c:a", AUDIO_CODEC, "-b:a", TARGET_AUDIO_BITRATE, "-ac", str(AUDIO_CHANNELS), "-ar", str(AUDIO_SAMPLE_RATE),
                        ] + movflags_ts2 + [str(temp_output)]
                        proc = await asyncio.create_subprocess_exec(
                            *cmd_a, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )
                        if user_id and getattr(self, 'task_tracker', None):
                            self.task_tracker.register_process(user_id, proc)
                        try:
                            if progress_callback:
                                asyncio.create_task(self._monitor_ffmpeg_progress(proc, temp_output, progress_callback))
                            await asyncio.wait_for(proc.communicate(), timeout=PROCESSING_TIMEOUT)
                        finally:
                            if user_id and getattr(self, 'task_tracker', None):
                                self.task_tracker.unregister_process(user_id, proc)

                    if not temp_output.exists() or temp_output.stat().st_size == 0:
                        # Fallback: copy-only merge tanpa re-encode
                        logger.warning("TS re-encode merge failed, trying copy-only merge...")
                        movflags_copy = ["-movflags", MOVFLAGS] if output_format.lower() == "mp4" else []
                        cmd_copy = [
                            "ffmpeg", "-y",
                            "-f", "concat", "-safe", "0", "-i", str(concat_file),
                            "-c", "copy",
                        ] + movflags_copy + [str(temp_output)]
                        proc = await asyncio.create_subprocess_exec(
                            *cmd_copy, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                        )
                        if user_id and getattr(self, 'task_tracker', None):
                            self.task_tracker.register_process(user_id, proc)
                        try:
                            _, stderr_copy = await asyncio.wait_for(proc.communicate(), timeout=PROCESSING_TIMEOUT)
                        finally:
                            if user_id and getattr(self, 'task_tracker', None):
                                self.task_tracker.unregister_process(user_id, proc)
                        if not temp_output.exists() or temp_output.stat().st_size == 0:
                            logger.error(f"Failed to create video (TS path) — copy-only also failed")
                            if stderr_copy:
                                logger.error(f"ffmpeg stderr: {stderr_copy.decode(errors='replace')[-500:]}")
                            return None


            # ── Validasi ─────────────────────────────────────────────────────
            if not temp_output.exists() or temp_output.stat().st_size == 0:
                logger.error("Merge output missing or empty")
                return None

            duration = await self._get_video_duration(temp_output)
            logger.info(f"✅ Final video duration: {duration:.2f} seconds")

            file_size_mb = temp_output.stat().st_size / (1024 * 1024)
            logger.info(f"📦 Final video size: {file_size_mb:.2f} MB")

            # ── Kompres jika perlu ────────────────────────────────────────────
            if ENABLE_AUTO_COMPRESS and file_size_mb > TARGET_FILE_SIZE_MB:
                logger.info(f"Compressing {file_size_mb:.2f}MB → <{TARGET_FILE_SIZE_MB}MB...")
                compressed = await self._compress_video(temp_output, output_path, progress_callback)
                if compressed:
                    return compressed
                else:
                    shutil.move(str(temp_output), str(output_path))
                    return output_path
            else:
                shutil.move(str(temp_output), str(output_path))
                return output_path

        except Exception as e:
            logger.error(f"Error in download_stream: {e}")
            import traceback
            traceback.print_exc()
            return None
        finally:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
                if temp_dir in self._temp_dirs:
                    self._temp_dirs.remove(temp_dir)
            except:
                pass

    async def _download_segments_parallel(self, segments: List[str], temp_dir: Path, 
                                         label: str, progress_callback: Optional[Callable],
                                         user_id: Optional[int] = None) -> List[Path]:
        """
        Download segmen secara paralel dengan retry mechanism
        """
        if not temp_dir.exists():
            temp_dir.mkdir(parents=True, exist_ok=True)
        
        downloaded_paths = []
        semaphore = asyncio.Semaphore(SEGMENT_CONCURRENCY)
        session = await self._get_session()
        
        async def _download_one(url: str, index: int) -> Optional[Path]:
            # Deteksi ekstensi
            url_path = url.split("?")[0].lower()
            if url_path.endswith(".m4s") or url_path.endswith(".mp4"):
                ext = ".m4s"
            elif url_path.endswith(".aac") or url_path.endswith(".m4a"):
                ext = ".m4a"
            else:
                ext = ".ts"
                
            file_name = f"{label}_{index:05d}{ext}"
            target_path = temp_dir / file_name
            
            async with semaphore:
                for attempt in range(MAX_SEGMENT_RETRIES):
                    try:
                        # Fallback ke aria2c jika download direct sering gagal
                        # Namun untuk segment kecil, aiohttp lebih efisien
                        headers = self._build_headers(url)
                        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=SEGMENT_TIMEOUT), proxy=DOWNLOAD_PROXY) as resp:
                            if resp.status == 200:
                                content = await resp.read()
                                if content:
                                    # Shortmax decryption support
                                    if ShortmaxDecryptor and content.startswith(b'shortmax'):
                                        content = ShortmaxDecryptor.decrypt_segment(content)
                                        
                                    async with aiofiles.open(target_path, 'wb') as f:
                                        await f.write(content)
                                    return target_path
                            elif resp.status in [401, 403] and attempt < MAX_SEGMENT_RETRIES - 1:
                                await asyncio.sleep(1)
                                continue
                        await asyncio.sleep(1)
                    except Exception as e:
                        logger.warning(f"Segment download error (attempt {attempt+1}): {e}")
                        await asyncio.sleep(1)
                return None

        tasks = [_download_one(url, i) for i, url in enumerate(segments)]
        results = await asyncio.gather(*tasks)
        
        downloaded_paths = [r for r in results if isinstance(r, Path) and r.exists()]
        logger.info(f"✅ {label}: Downloaded {len(downloaded_paths)}/{len(segments)} segments")
        
        return downloaded_paths

    async def _merge_segments(self, segments: List[Path], output_file: Path) -> bool:
        """Merge TS segments using ffmpeg concat"""
        if not segments:
            return False
        
        # Create concat file
        concat_file = output_file.parent / "concat.txt"
        async with aiofiles.open(concat_file, 'w') as f:
            for seg in segments:
                await f.write(f"file '{seg.absolute()}'\n")
        
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            str(output_file)
        ]
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            
            if proc.returncode != 0:
                stderr_str = stderr.decode()[:200] if stderr else ""
                logger.error(f"Merge failed: {stderr_str}")
                return False
            
            return output_file.exists() and output_file.stat().st_size > 0
            
        except asyncio.TimeoutError:
            logger.error("Merge timeout")
            return False
        except Exception as e:
            logger.error(f"Merge error: {e}")
            return False

    async def _merge_video_audio(self, video_file: Path, audio_file: Path,
                                 output_file: Path,
                                 progress_callback: Optional[Callable]) -> bool:
        """Merge video dan audio yang terpisah menjadi MP4"""
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_file),
            "-i", str(audio_file),
            "-c:v", "copy",
            "-c:a", AUDIO_CODEC,
            "-b:a", TARGET_AUDIO_BITRATE,
            "-movflags", MOVFLAGS,
            str(output_file)
        ]
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            if progress_callback:
                asyncio.create_task(self._monitor_ffmpeg_progress(
                    proc, output_file, progress_callback
                ))
            
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROCESSING_TIMEOUT)
            
            if proc.returncode != 0:
                stderr_str = stderr.decode()[:200] if stderr else ""
                logger.error(f"Merge video/audio failed: {stderr_str}")
                return False
            
            return output_file.exists() and output_file.stat().st_size > 0
            
        except Exception as e:
            logger.error(f"Merge video/audio error: {e}")
            return False

    async def _merge_video_audio_subtitle(self, video_file: Path, audio_file: Path,
                                          subtitle_file: Path, output_file: Path,
                                          progress_callback: Optional[Callable]) -> bool:
        """Merge video, audio, dan subtitle menjadi satu MP4"""
        
        # Method 1: Dengan embed subtitle sebagai track terpisah
        cmd1 = [
            "ffmpeg", "-y",
            "-i", str(video_file),
            "-i", str(audio_file),
            "-i", str(subtitle_file),
            "-c:v", "copy",
            "-c:a", AUDIO_CODEC,
            "-b:a", TARGET_AUDIO_BITRATE,
            "-c:s", "mov_text",  # Embed subtitle sebagai track
            "-metadata:s:s:0", "language=id",
            "-metadata:s:s:0", "title=Indonesian",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-map", "2:s:0",
            "-movflags", MOVFLAGS,
            str(output_file)
        ]
        
        # Method 2: Burn subtitle ke video (hardcode)
        cmd2 = [
            "ffmpeg", "-y",
            "-i", str(video_file),
            "-i", str(audio_file),
            "-vf", f"subtitles={subtitle_file}",
            "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
            "-preset", VIDEO_PRESET,
            "-c:a", AUDIO_CODEC,
            "-b:a", TARGET_AUDIO_BITRATE,
            "-movflags", MOVFLAGS,
            str(output_file)
        ]
        
        methods = [
            (cmd1, "Embed subtitle track"),
            (cmd2, "Burn subtitle")
        ]
        
        for cmd, method_name in methods:
            try:
                logger.info(f"Trying {method_name}...")
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                if progress_callback:
                    asyncio.create_task(self._monitor_ffmpeg_progress(
                        proc, output_file, progress_callback
                    ))
                
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROCESSING_TIMEOUT)
                
                if proc.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
                    logger.info(f"✅ {method_name} succeeded")
                    return True
                else:
                    stderr_str = stderr.decode()[:200] if stderr else ""
                    logger.warning(f"{method_name} failed: {stderr_str}")
                    
            except asyncio.TimeoutError:
                logger.warning(f"{method_name} timeout")
            except Exception as e:
                logger.warning(f"{method_name} error: {e}")
        
        return False

    async def _burn_subtitle_to_video(self, video_file: Path, subtitle_file: Path,
                                      output_file: Path,
                                      progress_callback: Optional[Callable]) -> bool:
        """Burn subtitle ke video (tanpa audio terpisah)"""
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_file),
            "-vf", f"subtitles={subtitle_file}",
            "-c:a", "copy",
            "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
            "-preset", VIDEO_PRESET,
            "-movflags", MOVFLAGS,
            str(output_file)
        ]
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            if progress_callback:
                asyncio.create_task(self._monitor_ffmpeg_progress(
                    proc, output_file, progress_callback
                ))
            
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROCESSING_TIMEOUT)
            
            if proc.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
                return True
            
            return False
        except Exception as e:
            logger.error(f"Burn subtitle error: {e}")
            return False

    async def _convert_to_mp4(self, input_file: Path, output_file: Path,
                              progress_callback: Optional[Callable]) -> bool:
        """Convert TS ke MP4 dengan multiple fallback methods"""
        
        # Method 1: Standard conversion dengan libx264
        cmd1 = [
            "ffmpeg", "-y",
            "-i", str(input_file),
            "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
            "-preset", VIDEO_PRESET,
            "-c:a", AUDIO_CODEC,
            "-b:a", TARGET_AUDIO_BITRATE,
            "-movflags", MOVFLAGS,
            str(output_file)
        ]
        
        # Method 2: Copy codec (tercepat)
        cmd2 = [
            "ffmpeg", "-y",
            "-i", str(input_file),
            "-c", "copy",
            "-movflags", MOVFLAGS,
            str(output_file)
        ]
        
        # Method 3: MP4 muxer with h264
        cmd3 = [
            "ffmpeg", "-y",
            "-i", str(input_file),
            "-f", "mp4",
            "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
            "-preset", VIDEO_PRESET,
            "-c:a", AUDIO_CODEC,
            "-b:a", "96k",
            "-movflags", MOVFLAGS,
            str(output_file)
        ]
        
        # Method 4: Gunakan format asli
        cmd4 = [
            "ffmpeg", "-y",
            "-i", str(input_file),
            "-codec", "copy",
            "-f", "mp4",
            str(output_file)
        ]
        
        methods = [
            (cmd1, "Standard libx264"),
            (cmd2, "Copy codec"),
            (cmd3, "Ultrafast preset"),
            (cmd4, "Format copy")
        ]
        
        for cmd, method_name in methods:
            try:
                logger.info(f"Trying {method_name} conversion...")
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                if progress_callback:
                    asyncio.create_task(self._monitor_ffmpeg_progress(
                        proc, output_file, progress_callback
                    ))
                
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROCESSING_TIMEOUT)
                
                if proc.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
                    logger.info(f"✅ {method_name} conversion succeeded")
                    return True
                else:
                    stderr_str = stderr.decode()[:200] if stderr else ""
                    logger.warning(f"{method_name} failed: {stderr_str}")
                    
            except asyncio.TimeoutError:
                logger.warning(f"{method_name} timeout")
            except Exception as e:
                logger.warning(f"{method_name} error: {e}")
        
        logger.error("All conversion methods failed")
        return False

    async def _compress_video(self, input_file: Path, output_file: Path,
                              progress_callback: Optional[Callable]) -> Optional[Path]:
        """
        Kompres video hingga ukuran < 50MB
        Strategi: turunkan bitrate bertahap
        """
        target_size_bytes = TARGET_FILE_SIZE_MB * 1024 * 1024
        current_size = input_file.stat().st_size
        
        # Hitung bitrate yang diperlukan
        duration = await self._get_video_duration(input_file)
        if duration <= 0:
            duration = 300  # Default 5 menit jika gagal
        
        # Target bitrate = (target_size * 8) / duration
        target_bitrate = int((target_size_bytes * 8) / duration)
        
        # Konversi ke kbps untuk ffmpeg
        video_bitrate = max(100, min(2000, target_bitrate // 1000))  # antara 100k - 2000k
        
        logger.info(f"Compressing: duration={duration:.2f}s, target_bitrate={video_bitrate}k")
        
        # Coba kompres dengan bitrate target
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_file),
            "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
            "-preset", VIDEO_PRESET,
            "-b:v", f"{video_bitrate}k",
            "-maxrate", f"{video_bitrate*2}k",
            "-bufsize", f"{video_bitrate*4}k",
            "-c:a", AUDIO_CODEC,
            "-b:a", TARGET_AUDIO_BITRATE,
            "-movflags", MOVFLAGS,
            str(output_file)
        ]
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            if progress_callback:
                asyncio.create_task(self._monitor_ffmpeg_progress(
                    proc, output_file, progress_callback
                ))
            
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROCESSING_TIMEOUT)
            
            if proc.returncode != 0 or not output_file.exists():
                logger.error("Compression failed")
                return None
            
            # Cek ukuran final
            final_size = output_file.stat().st_size
            if final_size <= target_size_bytes:
                logger.info(f"Compression successful: {final_size/(1024*1024):.2f}MB")
                return output_file
            else:
                # Masih > 50MB, coba kompres lagi dengan bitrate lebih rendah
                logger.warning(f"Still >{TARGET_FILE_SIZE_MB}MB ({final_size/(1024*1024):.2f}MB), trying lower bitrate")
                
                # Coba dengan bitrate 70% dari sebelumnya
                lower_bitrate = int(video_bitrate * 0.7)
                return await self._compress_video_lower_bitrate(
                    input_file, output_file, lower_bitrate, progress_callback
                )
                
        except Exception as e:
            logger.error(f"Compression error: {e}")
            return None

    async def _compress_video_lower_bitrate(self, input_file: Path, output_file: Path,
                                           bitrate_k: int,
                                           progress_callback: Optional[Callable]) -> Optional[Path]:
        """Kompres dengan bitrate lebih rendah"""
        if bitrate_k < 100:  # Minimal 100k
            logger.warning("Bitrate too low, giving up")
            return None
        
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_file),
            "-c:v", VIDEO_CODEC, "-profile:v", VIDEO_PROFILE, "-level:v", VIDEO_LEVEL, "-preset", VIDEO_PRESET, "-tune", VIDEO_TUNE, "-b:v", TARGET_VIDEO_BITRATE, "-maxrate", TARGET_VIDEO_MAXRATE, "-bufsize", TARGET_VIDEO_BUFSIZE, "-pix_fmt", PIX_FMT, "-color_primaries", COLOR_PRIMARIES, "-color_trc", COLOR_TRC, "-colorspace", COLORSPACE, "-color_range", COLOR_RANGE, "-x264-params", X264_PARAMS,
            "-preset", VIDEO_PRESET,
            "-b:v", f"{bitrate_k}k",
            "-maxrate", f"{bitrate_k*2}k",
            "-bufsize", f"{bitrate_k*4}k",
            "-c:a", AUDIO_CODEC,
            "-b:a", "96k",  # Turunkan audio bitrate juga
            "-movflags", MOVFLAGS,
            str(output_file)
        ]
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            if progress_callback:
                asyncio.create_task(self._monitor_ffmpeg_progress(
                    proc, output_file, progress_callback
                ))
            
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=PROCESSING_TIMEOUT)
            
            if proc.returncode != 0 or not output_file.exists():
                return None
            
            final_size = output_file.stat().st_size
            if final_size <= TARGET_FILE_SIZE_MB * 1024 * 1024:
                return output_file
            else:
                # Rekursif dengan bitrate lebih rendah
                return await self._compress_video_lower_bitrate(
                    input_file, output_file, int(bitrate_k * 0.8), progress_callback
                )
                
        except Exception as e:
            logger.error(f"Lower bitrate compression error: {e}")
            return None

    async def _get_video_duration(self, video_file: Path) -> float:
        """Get video duration using ffprobe"""
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(video_file)
        ]
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            
            if proc.returncode == 0:
                data = json.loads(stdout)
                # Cari durasi dari format
                duration = float(data.get("format", {}).get("duration", 0))
                return duration
        except:
            pass
        
        return 0

    async def _monitor_ffmpeg_progress(self, process, output_file: Path, callback: Callable):
        """Monitor progress ffmpeg dengan membaca output file size"""
        last_size = 0
        stall_count = 0
        
        while process.returncode is None:
            await asyncio.sleep(1)
            if output_file.exists():
                current_size = output_file.stat().st_size
                if current_size > last_size:
                    try:
                        await callback(current_size)
                    except:
                        pass
                    last_size = current_size
                    stall_count = 0
                else:
                    stall_count += 1
                    if stall_count > 30:  # Stall > 30 detik
                        logger.warning("FFmpeg stalled, terminating...")
                        process.terminate()
                        break

    async def download_subtitle(self, url: str, output_path: Path) -> Optional[Path]:
        """Download subtitle file dengan validasi"""
        try:
            session = await self._get_session()
            headers = self._build_headers(url)
            
            async with session.get(url, headers=headers, proxy=DOWNLOAD_PROXY) as resp:
                if resp.status != 200:
                    return None
                
                content = await resp.read()
                if not content:
                    return None
                
                # Validasi konten subtitle
                sample = content[:500].decode('utf-8', errors='ignore')
                
                # Cek apakah ini HLS playlist (bukan subtitle)
                if sample.startswith("#EXTM3U") or "#EXT-X-" in sample:
                    logger.warning("URL mengarah ke HLS playlist, bukan subtitle")
                    return None
                
                # Deteksi format
                if "WEBVTT" in sample:
                    suffix = ".vtt"
                elif "-->" in sample:
                    suffix = ".srt"
                elif "[Script Info]" in sample:
                    suffix = ".ass"
                else:
                    logger.warning("Unknown subtitle format")
                    return None
                
                # Set output path dengan ekstensi yang benar
                if output_path.suffix.lower() != suffix:
                    output_path = output_path.with_suffix(suffix)
                
                # Simpan file
                async with aiofiles.open(output_path, 'wb') as f:
                    await f.write(content)
                
                # Persistent copy for Indonesian subs (requested by user)
                try:
                    from config import SUBTITLE_DIR, COLLECT_SUBTITLES, SUBTITLE_REGISTRY_FILE
                    import json
                    from datetime import datetime
                    
                    if COLLECT_SUBTITLES:
                        persistent_name = output_path.name
                        # If output_path is generic "subtitle.vtt", try to use something better
                        if persistent_name.startswith("subtitle"):
                            persistent_name = f"sub_{int(asyncio.get_event_loop().time())}{suffix}"
                        
                        persistent_path = SUBTITLE_DIR / persistent_name
                        async with aiofiles.open(persistent_path, 'wb') as f:
                            await f.write(content)
                        
                        # Use JSON registry
                        registry = {}
                        if SUBTITLE_REGISTRY_FILE.exists():
                            try:
                                with open(SUBTITLE_REGISTRY_FILE, 'r', encoding='utf-8') as rf:
                                    registry = json.load(rf)
                            except:
                                registry = {}
                        
                        # Record with "id" language code
                        registry[persistent_name] = {
                            "url": url,
                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "language": "id",
                            "format": suffix.replace(".", ""),
                            "file_path": str(persistent_path)
                        }
                        
                        with open(SUBTITLE_REGISTRY_FILE, 'w', encoding='utf-8') as wf:
                            json.dump(registry, wf, indent=4, ensure_ascii=False)
                            
                        logger.info(f"✅ Persistent subtitle and JSON registry entry saved: {persistent_name}")
                except Exception as e:
                    logger.warning(f"Failed to save persistent subtitle copy or JSON registry: {e}")

                logger.info(f"Subtitle downloaded: {output_path.name}")
                return output_path
                
        except Exception as e:
            logger.error(f"Subtitle download error: {e}")
            return None

    async def close(self):
        """Cleanup resources"""
        # Bersihkan semua temporary directories
        for temp_dir in self._temp_dirs:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except:
                pass
        self._temp_dirs.clear()
        
        # Close session
        if self._session and not self._session.closed:
            await self._session.close()