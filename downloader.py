import asyncio
import yt_dlp
from pathlib import Path
from typing import Optional, Callable, List, Dict
from urllib.parse import urlparse

from utils import logger, get_headers, SubtitleDetector
from config import CLEANUP_ON_ERROR, DOWNLOAD_PROXY
from hls_downloader import OptimizedHLSDownloader, HLSStreamInfo

from task_tracker import TaskTracker

class DownloadManager:
    """
    Download Manager dengan HLS optimization dan multiple fallback methods
    """
    
    def __init__(self, task_tracker: Optional[TaskTracker] = None):
        self.hls_downloader = OptimizedHLSDownloader(task_tracker)
        self.task_tracker = task_tracker
        self.active_downloads: Dict[str, asyncio.Task] = {}
        
    async def download_video(self, url: str, output_path: Path,
                            user_id: int,
                            progress_callback: Optional[Callable] = None,
                            headers: Optional[Dict] = None,
                            subtitle_mode: str = "none",
                            subtitle_url: Optional[str] = None,
                            target_resolution: str = "1080p",
                            output_format: str = "mp4") -> Optional[Path]:
        """
        Download video dengan auto detection dan multiple fallback methods
        subtitle_mode: none, embed, separate
        target_resolution: 1080p, 720p, dst
        output_format: mp4, mkv
        """
        try:
            # Get optimized headers
            req_headers = get_headers(url)
            if headers:
                req_headers.update(headers)
                
            # Deteksi tipe URL
            if self._is_hls(url):
                logger.info(f"🎬 Detected HLS stream: {url[:100]}")
                
                # Method 1: Gunakan HLS downloader internal
                stream_info = await self.hls_downloader.analyze_stream(url)
                if stream_info:
                    await self.hls_downloader.apply_variant(stream_info, target_resolution)
                    burn_subtitle = (subtitle_mode == "embed")
                    hls_format = output_format
                    if hasattr(output_path, 'suffix') and output_path.suffix.lower() == ".mkv":
                        hls_format = "mkv"
                    
                    result = await self.hls_downloader.download_stream(
                        stream_info=stream_info,
                        output_path=output_path,
                        user_id=user_id,
                        progress_callback=progress_callback,
                        burn_subtitle=burn_subtitle,
                        subtitle_url=subtitle_url if subtitle_mode != "none" else None,
                        output_format=hls_format
                    )
                    if result and result.exists():
                        return result
                    logger.warning("Internal HLS downloader failed, trying yt-dlp...")
                
                # Method 2: yt-dlp fallback untuk HLS
                result = await self._download_with_ytdlp(
                    url, output_path, user_id, progress_callback, 
                    is_hls=True, output_format=output_format, 
                    headers=req_headers, target_resolution=target_resolution
                )
                if result:
                    return result
                
                # Method 3: ffmpeg direct HLS download
                return await self._download_hls_with_ffmpeg(url, output_path, user_id, progress_callback, output_format=output_format)
                
            else:
                # Regular file download
                logger.info(f"🎬 Detected regular file: {url[:100]}")
                
                # Step 1: yt-dlp primary
                logger.info("🎬 Trying yt-dlp as primary download engine...")
                result = await self._download_with_ytdlp(
                    url, output_path, user_id, progress_callback, 
                    is_hls=False, output_format=output_format, 
                    headers=req_headers, target_resolution=target_resolution
                )
                if result:
                    return result
                
                # Step 2: aria2c turbo fallback
                logger.warning("yt-dlp failed, trying aria2c turbo fallback...")
                result = await self._download_aria2_turbo(url, output_path, user_id, progress_callback, req_headers, output_format=output_format)
                if result:
                    return result
                
                # Step 3: Python requests fallback streaming download
                logger.warning("aria2c failed, trying requests streaming fallback...")
                return await self._download_streaming_requests(url, output_path, user_id, progress_callback, req_headers, output_format=output_format)
                
        except Exception as e:
            logger.error(f"Download failed: {e}")
            return None

    async def _download_with_ytdlp(self, url: str, output_path: Path,
                                   user_id: int,
                                   progress_callback: Optional[Callable],
                                   is_hls: bool = False,
                                   output_format: str = "mp4",
                                   headers: Optional[Dict] = None,
                                   target_resolution: str = "1080p") -> Optional[Path]:
        """Download menggunakan yt-dlp library dengan advanced options"""
        try:
            logger.info(f"🎬 Trying yt-dlp direct library: {url[:100]}")
            
            # Capture the current loop to use in the progress hook

            # Capture the current loop to use in the progress hook
            loop = asyncio.get_running_loop()

            # Progress hook for yt-dlp
            def ydl_progress_hook(d):
                if d['status'] == 'downloading':
                    try:
                        downloaded = d.get('downloaded_bytes', 0)
                        if progress_callback and downloaded:
                            # Use run_coroutine_threadsafe since ydl runs in a separate thread
                            asyncio.run_coroutine_threadsafe(progress_callback(downloaded), loop)
                    except Exception:
                        pass

            # Setup options
            # Convert target_resolution to height (e.g. "1080p" -> 1080)
            target_height = 1080
            import re as _re
            _res_match = _re.search(r'(\d+)', target_resolution)
            if _res_match:
                target_height = int(_res_match.group(1))

            # Quality format selection logic
            if is_hls:
                # Optimized for HLS: select specific height if possible
                fmt_str = (
                    f"bestvideo[height<={target_height}][ext={output_format}]+bestaudio[ext=m4a]/"
                    f"bestvideo[height<={target_height}]+bestaudio/"
                    f"best[height<={target_height}][ext={output_format}]/"
                    f"best[height<={target_height}]/"
                    f"best"
                )
            else:
                # Regular video quality selection
                fmt_str = (
                    f"bestvideo[height<={target_height}]+bestaudio/best[height<={target_height}]/best"
                )

            ydl_opts = {
                "format": fmt_str,
                "outtmpl": str(output_path),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "merge_output_format": output_format,
                "http_headers": {
                    "User-Agent": headers.get('User-Agent', "Mozilla/5.0"),
                    "Referer": headers.get('Referer', "https://www.flickreels.net/"),
                    "Accept": "*/*"
                },
                "extractor_args": {
                    "generic": ["impersonate"]
                },
                "impersonate": "chrome",
                "nocheckcertificate": True,
                "retries": 5,
                "fragment_retries": 5,
                "file_access_retries": 5,
                "concurrent_fragment_downloads": 10,
                "progress_hooks": [ydl_progress_hook],
            }

            if DOWNLOAD_PROXY:
                ydl_opts["proxy"] = DOWNLOAD_PROXY
                logger.info(f"🌐 Using proxy for yt-dlp: {DOWNLOAD_PROXY}")

            # Add cookies if exists
            cookies_file = Path("cookies.txt")
            if cookies_file.exists():
                ydl_opts["cookiefile"] = str(cookies_file)
                logger.info("🍪 Using cookies.txt for yt-dlp")

            # Run in thread pool to not block event loop
            loop = asyncio.get_event_loop()
            
            def _run_ydl():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    return ydl.download([url])

            return_code = await loop.run_in_executor(None, _run_ydl)
            
            if return_code == 0 and output_path.exists() and output_path.stat().st_size > 0:
                logger.info(f"✅ yt-dlp library download successful: {output_path.name}")
                return output_path
            else:
                logger.error(f"yt-dlp library failed with code {return_code}")
                return None
                
        except Exception as e:
            logger.error(f"yt-dlp library error: {e}")
            return None

    async def _download_direct_http(self, url: str, output_path: Path,
                                    user_id: int,
                                    progress_callback: Optional[Callable],
                                    headers: Optional[Dict] = None,
                                    output_format: str = "mp4") -> Optional[Path]:
        """Download file langsung via aiohttp untuk direct MP4/video URL"""
        try:
            import aiohttp
            import aiofiles
            logger.info(f"🌐 Trying direct HTTP download: {url[:100]}")
            
            if not headers:
                headers = get_headers(url)

            req_headers = {
                "User-Agent": headers.get('User-Agent', "Mozilla/5.0"),
                "Referer": headers.get('Referer', url),
                "Accept": "video/mp4,video/*,*/*",
                "Accept-Encoding": "identity",
            }
            
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=3600, connect=30)
            
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(url, headers=req_headers, allow_redirects=True, proxy=DOWNLOAD_PROXY) as resp:
                    if resp.status != 200:
                        logger.warning(f"Direct HTTP failed: status {resp.status}")
                        return None
                    
                    total = int(resp.headers.get('Content-Length', 0))
                    downloaded = 0
                    
                    async with aiofiles.open(output_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            await f.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback and downloaded % (5 * 1024 * 1024) < (1024 * 1024):
                                try:
                                    await progress_callback(downloaded)
                                except:
                                    pass
            
            if output_path.exists() and output_path.stat().st_size > 0:
                logger.info(f"✅ Direct HTTP download OK: {output_path.name} ({output_path.stat().st_size} bytes)")
                return output_path
            return None
            
        except Exception as e:
            logger.error(f"Direct HTTP download error: {e}")
            if output_path.exists():
                output_path.unlink()
            return None

    async def _download_hls_with_ffmpeg(self, url: str, output_path: Path,
                                         user_id: int,
                                         progress_callback: Optional[Callable],
                                         output_format: str = "mp4") -> Optional[Path]:
        """Download HLS stream langsung pakai ffmpeg sebagai last resort"""
        try:
            logger.info(f"🎬 Trying ffmpeg direct HLS download: {url[:100]}")
            
            if output_format.lower() == "mkv" and output_path.suffix.lower() != ".mkv":
                output_path = output_path.with_suffix(".mkv")

            cmd = [
                "ffmpeg", "-y",
                "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "-headers", "Accept: application/vnd.apple.mpegurl\r\n",
                "-i", url,
                "-c", "copy"
            ]
            
            if DOWNLOAD_PROXY:
                cmd.insert(1, "-http_proxy")
                cmd.insert(2, DOWNLOAD_PROXY)
            
            if output_format.lower() == "mp4":
               cmd.extend(["-movflags", "+faststart"])
               
            cmd.append(str(output_path))
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            if user_id and getattr(self, 'task_tracker', None):
                self.task_tracker.register_process(user_id, process)
                
            try:
                if progress_callback:
                    asyncio.create_task(self._monitor_file_progress(
                        output_path, progress_callback
                    ))
                
                stdout, stderr = await process.communicate()
            finally:
                if user_id and getattr(self, 'task_tracker', None):
                    self.task_tracker.unregister_process(user_id, process)
            
            if output_path.exists() and output_path.stat().st_size > 0:
                logger.info(f"✅ ffmpeg HLS download OK: {output_path.name}")
                return output_path
            
            stderr_str = stderr.decode()[:300] if stderr else ""
            logger.error(f"ffmpeg HLS download failed: {stderr_str}")
            return None
            
        except Exception as e:
            logger.error(f"ffmpeg HLS download error: {e}")
            return None
    
    async def _monitor_file_progress(self, file_path: Path, callback: Callable):
        """Monitor progress file download"""
        last_size = 0
        stall_count = 0
        
        while True:
            await asyncio.sleep(2)
            if file_path.exists():
                current_size = file_path.stat().st_size
                if current_size > last_size:
                    try:
                        await callback(current_size)
                    except:
                        pass
                    last_size = current_size
                    stall_count = 0
                else:
                    stall_count += 1
                    if stall_count > 30:
                        break
    
    async def detect_hls_subtitles(self, url: str) -> List[Dict]:
        """Deteksi subtitle dari HLS stream dengan prioritization Indonesian"""
        try:
            if self._is_hls(url):
                stream_info = await self.hls_downloader.analyze_stream(url)
                if stream_info:
                    # Return all tracks but mark/prioritize Indonesian if needed
                    # Note: analyze_stream already uses SubtitleDetector to mark tracks
                    return stream_info.subtitle_tracks
        except Exception as e:
            logger.warning(f"Subtitle detection failed: {e}")
        return []
    
    async def download_subtitle(self, url: str, output_path: Path) -> Optional[Path]:
        """Download subtitle file"""
        return await self.hls_downloader.download_subtitle(url, output_path)
    
    def _is_hls(self, url: str) -> bool:
        """Deteksi apakah URL adalah HLS stream"""
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parsed.query.lower()
        host = parsed.netloc.lower()
        
        if path.endswith(".m3u8"):
            return True
        if "rishort.com" in host and "/hls/" in path:
            return True
        if "workers.dev" in host and ("/hls/proxy" in path or "/hls/m3u8" in path):
            return True
        if "goodshort" in host and "/hls/" in path:
            return True
        return False
    
    async def _download_aria2_turbo(self, url: str, output_path: Path,
                                   user_id: int,
                                   progress_callback: Optional[Callable],
                                   headers: Optional[Dict],
                                   output_format: str = "mp4") -> Optional[Path]:
        """Step 1: aria2c turbo download dengan settings optimasi tinggi"""
        try:
            logger.info(f"🚀 Starting aria2c turbo download: {url[:100]}")
            
            if output_format.lower() == "mkv" and output_path.suffix.lower() != ".mkv":
                output_path = output_path.with_suffix(".mkv")

            cmd = [
                "aria2c",
                "-x", "16",
                "-s", "16",
                "-k", "1M",
                "--max-connection-per-server=16",
                "--min-split-size=1M",
                "--file-allocation=none",
                "--continue=true",
                "--summary-interval=0",
                "--console-log-level=error",
                "-d", str(output_path.parent),
                "-o", output_path.name,
                url
            ]
            
            if DOWNLOAD_PROXY:
                cmd.extend(["--all-proxy", DOWNLOAD_PROXY])
            
            if headers:
                for key, value in headers.items():
                    cmd.extend(["--header", f"{key}: {value}"])
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            if user_id and getattr(self, 'task_tracker', None):
                self.task_tracker.register_process(user_id, process)
                
            try:
                if progress_callback:
                    asyncio.create_task(self._monitor_file_progress(output_path, progress_callback))
                await process.communicate()
            finally:
                if user_id and getattr(self, 'task_tracker', None):
                    self.task_tracker.unregister_process(user_id, process)
            
            if process.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                logger.info(f"✅ aria2c turbo success: {output_path.name}")
                return output_path
            return None
        except Exception as e:
            logger.error(f"aria2c turbo error: {e}")
            return None

    async def _download_streaming_requests(self, url: str, output_path: Path,
                                          user_id: int,
                                          progress_callback: Optional[Callable],
                                          headers: Optional[Dict],
                                          output_format: str = "mp4") -> Optional[Path]:
        """Step 3: Streaming download via aiohttp (fallback terakhir)"""
        return await self._download_direct_http(url, output_path, user_id, progress_callback, headers, output_format)

    async def close(self):
        """Cleanup resources"""
        await self.hls_downloader.close()