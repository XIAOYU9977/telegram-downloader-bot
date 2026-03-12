import asyncio
from pathlib import Path
from typing import Optional, Callable, List, Dict
from urllib.parse import urlparse

from utils import logger
from config import CLEANUP_ON_ERROR
from hls_downloader import OptimizedHLSDownloader, HLSStreamInfo

class DownloadManager:
    """
    Download Manager dengan HLS optimization dan multiple fallback methods
    """
    
    def __init__(self):
        self.hls_downloader = OptimizedHLSDownloader()
        self.active_downloads: Dict[str, asyncio.Task] = {}
        
    async def download_video(self, url: str, output_path: Path,
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
            # Deteksi tipe URL
            if self._is_hls(url):
                logger.info(f"🎬 Detected HLS stream: {url[:100]}")
                
                # Method 1: Gunakan HLS downloader internal (dengan audio & subtitle)
                stream_info = await self.hls_downloader.analyze_stream(url)
                if stream_info:
                    # Tampilkan info stream
                    if stream_info.video_segments:
                        logger.info(f"📹 Video segments: {len(stream_info.video_segments)}")
                    if stream_info.audio_segments:
                        logger.info(f"🎵 Audio segments: {len(stream_info.audio_segments)} (separate)")
                    if stream_info.subtitle_tracks:
                        logger.info(f"📝 Subtitle tracks: {len(stream_info.subtitle_tracks)}")
                    
                    # Terapkan resolusi target ke manifest sebelum download
                    await self.hls_downloader.apply_variant(stream_info, target_resolution)

                    # Download dengan optimasi
                    burn_subtitle = (subtitle_mode == "embed")
                    # Derive output_format from function param
                    # (default "mp4" if not explicitly set by caller)
                    hls_format = output_format
                    if hasattr(output_path, 'suffix') and output_path.suffix.lower() == ".mkv":
                        hls_format = "mkv"
                    # Override with explicit target_resolution-sibling variable
                    # The caller may also pass output_format via download_video kwargs
                    result = await self.hls_downloader.download_stream(
                        stream_info=stream_info,
                        output_path=output_path,
                        progress_callback=progress_callback,
                        burn_subtitle=burn_subtitle,
                        subtitle_url=subtitle_url if burn_subtitle else None,
                        output_format=hls_format
                    )
                    if result and result.exists():
                        return result
                    logger.warning("Internal HLS downloader failed, trying yt-dlp...")
                
                # Method 2: yt-dlp fallback untuk HLS
                result = await self._download_with_ytdlp(url, output_path, progress_callback, is_hls=True, output_format=output_format)
                if result:
                    return result
                
                # Method 3: ffmpeg direct HLS download
                return await self._download_hls_with_ffmpeg(url, output_path, progress_callback, output_format=output_format)
                
            else:
                # Regular file download
                logger.info(f"🎬 Detected regular file: {url[:100]}")
                
                # Method 1: aria2 direct download
                result = await self._download_regular(url, output_path, progress_callback, headers, output_format=output_format)
                if result:
                    return result
                
                # Method 2: aiohttp direct download
                result = await self._download_direct_http(url, output_path, progress_callback, output_format=output_format)
                if result:
                    return result
                
                # Method 3: yt-dlp sebagai last resort
                return await self._download_with_ytdlp(url, output_path, progress_callback, is_hls=False, output_format=output_format)
                
        except Exception as e:
            logger.error(f"Download failed: {e}")
            # Final fallback ke yt-dlp
            return await self._download_with_ytdlp(url, output_path, progress_callback, is_hls=False, output_format=output_format)
    
    async def _download_with_ytdlp(self, url: str, output_path: Path,
                                   progress_callback: Optional[Callable],
                                   is_hls: bool = False,
                                   output_format: str = "mp4") -> Optional[Path]:
        """Download menggunakan yt-dlp sebagai fallback"""
        try:
            logger.info(f"🎬 Trying yt-dlp fallback: {url[:100]}")
            
            if is_hls:
                # Untuk HLS: ambil format terbaik yang tersedia
                format_selector = f"bestvideo[ext={output_format}]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext={output_format}]/best"
            else:
                # Untuk direct URL: paksa download langsung
                format_selector = f"best[ext={output_format}]/best"
            
            # Jika user meminta MKV tapi output_path berakhiran .mp4, ganti extensionnya.
            if output_format.lower() == "mkv" and output_path.suffix.lower() != ".mkv":
                output_path = output_path.with_suffix(".mkv")

            cmd = [
                "yt-dlp",
                "-f", format_selector,
                "-o", str(output_path),
                "--no-playlist",
                "--no-warnings",
                "--merge-output-format", output_format,
                "--hls-prefer-native",
                url
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            if progress_callback:
                asyncio.create_task(self._monitor_file_progress(
                    output_path, progress_callback
                ))
            
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                logger.info(f"✅ yt-dlp download successful: {output_path.name}")
                return output_path
            else:
                stderr_str = stderr.decode()[:300] if stderr else ""
                logger.error(f"yt-dlp failed: {stderr_str}")
                return None
                
        except Exception as e:
            logger.error(f"yt-dlp error: {e}")
            return None

    async def _download_direct_http(self, url: str, output_path: Path,
                                    progress_callback: Optional[Callable],
                                    output_format: str = "mp4") -> Optional[Path]:
        """Download file langsung via aiohttp untuk direct MP4/video URL"""
        try:
            import aiohttp
            import aiofiles
            logger.info(f"🌐 Trying direct HTTP download: {url[:100]}")
            
            # Ganti ekstensi file jika format adalah mkv
            if output_format.lower() == "mkv" and output_path.suffix.lower() != ".mkv":
                output_path = output_path.with_suffix(".mkv")

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "video/mp4,video/*,*/*",
                "Accept-Encoding": "identity",  # Hindari gzip agar bisa hitung size
            }
            
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=3600, connect=30)
            
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(url, headers=headers, allow_redirects=True) as resp:
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
                                         progress_callback: Optional[Callable],
                                         output_format: str = "mp4") -> Optional[Path]:
        """Download HLS stream langsung pakai ffmpeg sebagai last resort"""
        try:
            logger.info(f"🎬 Trying ffmpeg direct HLS download: {url[:100]}")
            
            # Jika user meminta MKV tapi output_path berakhiran .mp4, ganti extensionnya.
            if output_format.lower() == "mkv" and output_path.suffix.lower() != ".mkv":
                output_path = output_path.with_suffix(".mkv")

            cmd = [
                "ffmpeg", "-y",
                "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "-headers", "Accept: application/vnd.apple.mpegurl\r\n",
                "-i", url,
                "-c", "copy"
            ]
            
            # faststart hanya untuk MP4
            if output_format.lower() == "mp4":
               cmd.extend(["-movflags", "+faststart"])
               
            cmd.append(str(output_path))
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            if progress_callback:
                asyncio.create_task(self._monitor_file_progress(
                    output_path, progress_callback
                ))
            
            stdout, stderr = await process.communicate()
            
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
        """Deteksi subtitle dari HLS stream"""
        try:
            if self._is_hls(url):
                stream_info = await self.hls_downloader.analyze_stream(url)
                if stream_info:
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
        
        # Cek ekstensi .m3u8
        if path.endswith(".m3u8"):
            return True
        
        # Rishort HLS API
        if "rishort.com" in host and "/hls/" in path:
            return True
        
        # HLS Proxy Cloudflare
        if "workers.dev" in host and ("/hls/proxy" in path or "/hls/m3u8" in path):
            return True
        
        # GoodShort HLS
        if "goodshort" in host and "/hls/" in path:
            return True
        
        return False
    
    async def _download_regular(self, url: str, output_path: Path,
                               progress_callback: Optional[Callable],
                               headers: Optional[Dict],
                               output_format: str = "mp4") -> Optional[Path]:
        """Download file biasa dengan aria2 (fallback)"""
        try:
            # Ganti ekstensi file jika format adalah mkv
            if output_format.lower() == "mkv" and output_path.suffix.lower() != ".mkv":
                output_path = output_path.with_suffix(".mkv")

            cmd = [
                "aria2c",
                "-x", "32",
                "-s", "32",
                "-k", "1M",
                "--file-allocation=none",
                "--min-split-size=1M",
                "--max-tries=5",
                "--retry-wait=2",
                "--max-file-not-found=5",
                "--continue=true",
                "--summary-interval=0",
                "--console-log-level=error",
                "--download-result=hide",
                "-d", str(output_path.parent),
                "-o", output_path.name,
                url
            ]
            
            if headers:
                for key, value in headers.items():
                    cmd.extend(["--header", f"{key}: {value}"])
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            if progress_callback:
                asyncio.create_task(self._monitor_file_progress(
                    output_path, progress_callback
                ))
            
            stdout, stderr = await process.communicate()
            
            if process.returncode != 0:
                stderr_str = stderr.decode() if stderr else "Unknown error"
                logger.error(f"aria2 download failed: {stderr_str[:200]}")
                return None
            
            if output_path.exists() and output_path.stat().st_size > 0:
                logger.info(f"Download completed: {output_path.name}")
                return output_path
            
        except Exception as e:
            logger.error(f"Regular download failed: {e}")
        
        return None
    
    async def close(self):
        """Cleanup resources"""
        await self.hls_downloader.close()