import asyncio
import json
import logging
import time
import re
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Union, Callable
import aiofiles
import aiohttp

from config import (
    SUPPORTED_SOURCES, CLEANUP_DELAY, CLEANUP_ON_ERROR,
    CLEANUP_JSON, CLEANUP_VIDEO, CLEANUP_SUBTITLE, CLEANUP_OUTPUT
)

try:
    from shortmax import ShortmaxParser
except ImportError:
    ShortmaxParser = None

try:
    from netshort import NetshortParser
except ImportError:
    NetshortParser = None

try:
    from vigloo import ViglooParser
except ImportError:
    ViglooParser = None

try:
    from flickreels.parser import FlickReelsParser
except ImportError:
    FlickReelsParser = None

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# Headers for specific sources
SOURCE_HEADERS = {
    "flickreels": {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.flickreels.net/",
        "Origin": "https://www.flickreels.net/"
    }
}

def get_headers(url: str) -> Dict[str, str]:
    """Get optimized headers for a specific URL"""
    from urllib.parse import urlparse
    domain = urlparse(url).netloc.lower()
    for key, headers in SOURCE_HEADERS.items():
        if key in domain:
            return headers.copy()
    
    # Default headers
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": url,
        "Accept": "*/*"
    }

# Universal JSON Parser for various drama video sources
class JSONParser:
    """Universal JSON Parser for various drama video sources"""
    
    @staticmethod
    def parse_universal(data: Union[Dict, List, str]) -> Dict[str, Any]:
        """
        Recursive search for video info in any JSON structure.
        Priority given to dramaflickreels format.
        """
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except:
                return {}

        result = {
            "title": "Drama Video",
            "episodes": [],
            "cover": None,
            "description": None,
            "source": "unknown"
        }

        # Check for dramaflickreels format
        if isinstance(data, dict):
            if "drama" in data and "episodes" in data:
                drama_info = data["drama"]
                result["title"] = drama_info.get("title", result["title"])
                result["cover"] = drama_info.get("cover")
                result["description"] = drama_info.get("description")
                result["source"] = drama_info.get("source", "dramaflickreels")
                
                for ep in data["episodes"]:
                    ep_info = {
                        "id": ep.get("id"),
                        "name": ep.get("name"),
                        "index": ep.get("index"),
                        "video_url": None,
                        "subtitle_url": None,
                        "cover": None
                    }
                    
                    # Extract from raw if available
                    raw = ep.get("raw", {})
                    if isinstance(raw, dict):
                        ep_info["video_url"] = raw.get("chapter_link") or raw.get("video_url") or raw.get("m3u8_url")
                        ep_info["cover"] = raw.get("chapter_cover") or raw.get("cover")
                    
                    if not ep_info["video_url"]:
                        ep_info["video_url"] = ep.get("video_url") or ep.get("url")
                        
                    if ep_info["video_url"]:
                        result["episodes"].append(ep_info)
                
                if result["episodes"]:
                    return result

        # General recursive search fallback
        all_videos = []
        all_subs = []
        
        def _find_recursively(obj):
            if isinstance(obj, dict):
                # Search for video URLs
                for k, v in obj.items():
                    if isinstance(v, str) and any(ext in v.lower() for ext in ['.m3u8', '.mp4', '.m4v']):
                        if v not in all_videos:
                            all_videos.append(v)
                    elif isinstance(v, (dict, list)):
                        _find_recursively(v)
                
                # Search for titles/covers
                if not result["cover"]:
                    result["cover"] = obj.get("cover") or obj.get("image") or obj.get("poster")
                if result["title"] == "Drama Video":
                    result["title"] = obj.get("title") or obj.get("drama_name") or obj.get("name", result["title"])

            elif isinstance(obj, list):
                for item in obj:
                    _find_recursively(item)

        _find_recursively(data)
        
        if all_videos and not result["episodes"]:
            for i, vid in enumerate(all_videos):
                result["episodes"].append({
                    "name": f"Episode {i+1}",
                    "index": i,
                    "video_url": vid
                })
        
        return result

    @staticmethod
    def extract_subtitle_id(data: Any) -> Optional[str]:
        """Detect Indonesian subtitle from varying structures"""
        # Multi-layer search for Indonesian language codes
        indonesian_codes = ["id", "ind", "indo", "bahasa", "indonesian"]
        
        def _check_lang(val):
            if not isinstance(val, str): return False
            val = val.lower()
            return any(code in val for code in indonesian_codes)

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    lang = item.get("language") or item.get("lang") or item.get("name")
                    if _check_lang(lang):
                        return item.get("url") or item.get("link") or item.get("src")
        
        return None

    @staticmethod
    def get_progress_bar(percentage: float, length: int = 20) -> str:
        """Generate a stylized progress bar: ████████░░░░ 65%"""
        percentage = max(0, min(100, percentage))
        filled = int(length * percentage / 100)
        bar = "█" * filled + "░" * (length - filled)
        return f"{bar} {percentage:.1f}%"

    @staticmethod
    def format_size(bytes_size: int) -> str:
        """Format bytes to human readable form"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_size < 1024:
                return f"{bytes_size:.2f} {unit}"
            bytes_size /= 1024
        return f"{bytes_size:.2f} TB"

    @staticmethod
    def format_speed(bytes_per_sec: float) -> str:
        """Format speed to human readable form"""
        return f"{JSONParser.format_size(int(bytes_per_sec))}/s"

# Daftar semua variasi kode subtitle Indonesia
INDONESIAN_SUBTITLE_CODES = [
    # Language codes
    "id", "id-ID", "id-id", "id_id", "ind", "in", "ID", "ID-ID", "in-ID", "in_ID",
    # Full names
    "indonesia", "indonesian", "bahasa", "bahasa_indonesia", "bahasa indonesia",
    "indonesian subtitle", "sub indo", "subtitle indonesia", "indo sub",
    # Common field names (prefixed/suffixed)
    "sub_id", "sub_ind", "subtitle_id", "subtitle_ind", "subtitle_indo",
    "sub_idn", "subtitle_idn", "sub_bahasa", "subtitles_id", "sub-id", "sub-ind",
    # Numeric codes (common in some APIs)
    "23", "102", "105",
    # Other variations
    "indonesian (id)", "id (indonesian)", "id-id (indonesian)",
    "id-ID (Indonesian)", "bahasa (id)", "indo", "indon", "id_ID"
]

# Keywords for official/premium subtitles
OFFICIAL_SUB_KEYWORDS = [
    "official", "resmi", "production", "original", "premium", "pro", "studio", "master"
]

class FileCleanup:
    """Utility class untuk auto cleanup files"""
    
    def __init__(self):
        self.files_to_cleanup = []
        self.cleanup_task = None
    
    @staticmethod
    async def safe_delete(file_path: Union[str, Path], delay: int = 0) -> bool:
        """
        Hapus file dengan aman
        Args:
            file_path: Path file yang akan dihapus
            delay: Delay sebelum hapus (detik)
        Returns:
            bool: True jika berhasil, False jika gagal
        """
        if not file_path:
            return False
        
        path = Path(file_path)
        
        if delay > 0:
            await asyncio.sleep(delay)
        
        try:
            if path.exists():
                if path.is_file():
                    path.unlink()
                    logger.info(f"✅ File dihapus: {path.name}")
                elif path.is_dir():
                    shutil.rmtree(path)
                    logger.info(f"✅ Folder dihapus: {path.name}")
                return True
            else:
                logger.debug(f"File tidak ditemukan: {path.name}")
                return False
        except PermissionError:
            logger.warning(f"❌ Tidak bisa menghapus {path.name} (Permission denied)")
            return False
        except Exception as e:
            logger.error(f"❌ Gagal menghapus {path.name}: {e}")
            return False
    
    @staticmethod
    async def cleanup_episode_files(
        video_path: Optional[Path] = None,
        subtitle_path: Optional[Path] = None,
        output_path: Optional[Path] = None,
        json_path: Optional[Path] = None,
        delay: int = CLEANUP_DELAY
    ):
        """
        Hapus semua file yang terkait dengan satu episode
        """
        logger.info(f"🧹 Membersihkan file episode... (delay {delay}s)")
        
        tasks = []
        
        if CLEANUP_VIDEO and video_path:
            tasks.append(FileCleanup.safe_delete(video_path, delay))
        
        if CLEANUP_SUBTITLE and subtitle_path:
            tasks.append(FileCleanup.safe_delete(subtitle_path, delay))
        
        if CLEANUP_OUTPUT and output_path:
            tasks.append(FileCleanup.safe_delete(output_path, delay))
        
        if CLEANUP_JSON and json_path:
            tasks.append(FileCleanup.safe_delete(json_path, delay))
        
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            success = sum(1 for r in results if r is True)
            logger.info(f"✅ {success} file berhasil dibersihkan")
    
    @staticmethod
    async def cleanup_batch_files(
        file_list: List[Path],
        delay: int = CLEANUP_DELAY,
        on_error: bool = False
    ):
        """
        Hapus banyak file sekaligus
        """
        if on_error and not CLEANUP_ON_ERROR:
            logger.info("🧹 Cleanup on error disabled, skipping...")
            return
        
        if not file_list:
            return
        
        logger.info(f"🧹 Membersihkan {len(file_list)} file...")
        await asyncio.sleep(delay)
        
        success = 0
        for file_path in file_list:
            if await FileCleanup.safe_delete(file_path):
                success += 1
        
        logger.info(f"✅ {success}/{len(file_list)} file dibersihkan")
    
    @staticmethod
    async def cleanup_old_files(directory: Path, minutes: int = 5):
        """
        Hapus file yang lebih lama dari X menit
        """
        if not directory.exists():
            return
        
        cutoff_time = time.time() - (minutes * 60)
        deleted = 0
        
        for file_path in directory.glob("*"):
            if file_path.is_file():
                try:
                    mtime = file_path.stat().st_mtime
                    if mtime < cutoff_time:
                        file_path.unlink()
                        deleted = deleted + 1  # type: ignore
                        logger.info(f"🧹 Hapus file lama: {file_path.name}")
                except Exception as e:
                    logger.error(f"Gagal hapus {file_path.name}: {e}")
        
        if deleted > 0:
            logger.info(f"✅ {deleted} file lama dibersihkan")


class SubtitleDetector:
    """Helper class to detect Indonesian subtitles with prioritization"""
    
    @staticmethod
    def is_indonesian_subtitle(subtitle_data: Dict[str, Any]) -> bool:
        """Check if subtitle data is for Indonesian language"""
        language_fields = [
            "language", "lang", "language_code", "lang_code", 
            "code", "languageId", "lang_id", "sub_lang", "subtitle_lang",
            "locale", "language_name", "name", "display_name", "title", "label"
        ]
        
        for field in language_fields:
            if field in subtitle_data and subtitle_data[field]:
                value = str(subtitle_data[field]).lower().strip()
                # Check for exact matches and inclusion
                for code in INDONESIAN_SUBTITLE_CODES:
                    code_lower = code.lower()
                    if value == code_lower or f"({code_lower})" in value or f" {code_lower}" in value:
                        logger.info(f"[SUB-DETECTION] Match found: {field}='{value}' corresponds to Indonesian ({code})")
                        return True
        
        # Additional check in URI/URL if no field matches
        url = SubtitleDetector.get_subtitle_url(subtitle_data)
        if url:
            url_lower = url.lower()
            if any(f"_{c}." in url_lower or f"-{c}." in url_lower or f"/{c}/" in url_lower for c in ["id", "ind", "indo"]):
                logger.info(f"[SUB-DETECTION] Match found in URL: Indonesian pattern detected in {url}")
                return True
        
        return False
    
    @staticmethod
    def is_official_subtitle(subtitle_data: Dict[str, Any]) -> bool:
        """Heuristic check for official/authoritative subtitle sources"""
        search_fields = ["name", "label", "title", "type", "category", "source", "author"]
        for field in search_fields:
            if field in subtitle_data and subtitle_data[field]:
                value = str(subtitle_data[field]).lower()
                if any(kw in value for kw in OFFICIAL_SUB_KEYWORDS):
                    logger.info(f"[SUB-DETECTION] Priority found: Subtitle identified as OFFICIAL via {field}='{value}'")
                    return True
        return False

    @staticmethod
    def find_indonesian_subtitle(subtitle_list: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Find the BEST Indonesian subtitle in a list.
        Priority: 1. Official Indonesian, 2. Any Indonesian
        """
        if not subtitle_list:
            return None
            
        indonesian_subs = [s for s in subtitle_list if SubtitleDetector.is_indonesian_subtitle(s)]
        
        if not indonesian_subs:
            return None
            
        if len(indonesian_subs) == 1:
            return indonesian_subs[0]
            
        # Try to find official one among Indonesian subs
        for sub in indonesian_subs:
            if SubtitleDetector.is_official_subtitle(sub):
                logger.info(f"[SUB-DETECTION] Multiple subs found, selecting official Indonesian subtitle.")
                return sub
                
        # Default to the first one found if no "official" tag
        logger.info(f"[SUB-DETECTION] Multiple subs found, selecting first available Indonesian subtitle.")
        return indonesian_subs[0]
    
    @staticmethod
    def get_subtitle_url(subtitle_data: Dict[str, Any]) -> Optional[str]:
        """Extract subtitle URL from subtitle data"""
        url_fields = [
            "url", "subtitle", "subtitle_url", "file", "path", "src",
            "link", "download_url", "sub", "sub_file", "subtitle_file",
            "sub_link", "subtitle_link", "srt", "vtt", "subtitle_path", "uri"
        ]
        
        for field in url_fields:
            if field in subtitle_data and subtitle_data[field]:
                url = subtitle_data[field]
                if isinstance(url, str) and (url.startswith(('http://', 'https://')) or url.endswith(('.vtt', '.srt', '.ass'))):
                    return url
        
        return None

class LocalSubtitleFinder:
    """Helper to find subtitles in local SUBTITLE_DIR"""
    
    @staticmethod
    def find_subtitle(drama_title: str, episode_num: Union[int, str]) -> Optional[Path]:
        """
        Search for a local subtitle matching drama title and episode
        """
        from config import SUBTITLE_DIR
        if not SUBTITLE_DIR.exists():
            return None
            
        try:
            # Clean title for matching
            def clean(s):
                # Remove special chars and spaces for robust matching
                return re.sub(r'[^a-zA-Z0-9]', '', str(s).lower())
            
            clean_drama = clean(drama_title)
            ep_str = str(episode_num).zfill(2)
            
            # Scan directory
            for sub_file in SUBTITLE_DIR.glob("*.*"):
                if sub_file.suffix.lower() not in ['.srt', '.vtt', '.ass']:
                    continue
                    
                filename = sub_file.name.lower()
                clean_filename = clean(filename)
                
                # Match logic:
                # 1. Drama title (cleaned) is in filename (cleaned)
                # 2. Episode number is in filename
                
                if clean_drama in clean_filename:
                    # Check for episode patterns: E01, EP01, _01, 01.srt
                    patterns = [
                        f"e{ep_str}", f"ep{ep_str}", f"episode{ep_str}",
                        f"_{ep_str}", f" {ep_str}", f"ep {ep_str}"
                    ]
                    
                    if any(pattern in filename for pattern in patterns) or f"{ep_str}." in filename:
                        logger.info(f"🔍 Found local subtitle match: {sub_file.name}")
                        return sub_file
                        
            return None
        except Exception as e:
            logger.error(f"Error in LocalSubtitleFinder: {e}")
            return None
        
        return None

    @staticmethod
    def get_progress_bar(percentage: float, length: int = 15) -> str:
        """
        Generate stylized progress bar: ████████░░░░ 65%
        """
        filled = int(length * percentage / 100)
        bar = "█" * filled + "░" * (length - filled)
        return f"{bar} {percentage:.1f}%"

    @staticmethod
    def format_speed(bps: float) -> str:
        """Format speed to human readable string"""
        if bps < 1024: return f"{bps:.0f} B/s"
        elif bps < 1024**2: return f"{bps/1024:.1f} KB/s"
        elif bps < 1024**3: return f"{bps/1024**2:.1f} MB/s"
        else: return f"{bps/1024**3:.1f} GB/s"


class JSONParser:
    """Parse various JSON formats from different sources"""
    
    @staticmethod
    async def parse_json_file(file_path: Path) -> Optional[Dict[str, Any]]:
        """Parse JSON file and return data"""
        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                content = await f.read()
                return json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            return None
        except Exception as e:
            logger.error(f"Error reading JSON file: {e}")
            return None
    
    @staticmethod
    def extract_video_url(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract video URL and subtitle URL from JSON data
        Returns (video_url, subtitle_url)
        """
        video_url: Optional[str] = None
        subtitle_url: Optional[str] = None
        
        # Try to detect source format
        data_dict = data.get("data")
        if "videos" in data and "shortPlayId" not in data:  # goodshort format
            return JSONParser._parse_goodshort(data)
        elif ("videoUrl" in data or "shortPlayId" in data) and ShortmaxParser: # shortmax format
            logger.info("Detected shortmax format")
            parsed = ShortmaxParser.parse(data)
            if parsed["episodes"]:
                ep = parsed["episodes"][0]
                return ep["url"], None
        elif "shortPlayEpisodeInfos" in data and NetshortParser: # netshort format
            logger.info("Detected netshort format")
            parsed = NetshortParser.parse(data)
            if parsed["episodes"]:
                ep = parsed["episodes"][0]
                return ep["url"], None
        elif "videoInfo" in data and "episodesInfo" in data:  # velolo format
            logger.info("Detected velolo format")
            return JSONParser._parse_velolo(data)
        elif "data" in data and isinstance(data.get("data"), dict):
            data_dict = data["data"]
            
            # Dramabox v2 - cek dulu karena punya episodes array
            if "episodes" in data_dict and isinstance(data_dict["episodes"], list):
                logger.info("Detected dramabox v2 format")
                return JSONParser._parse_dramabox_v2(data)
            elif "list" in data_dict:  # dramabox v1 format
                return JSONParser._parse_dramabox(data)
            elif "info" in data_dict:  # dramawave format
                return JSONParser._parse_dramawave(data)
            elif "play_url" in data_dict:  # meloshort format
                return JSONParser._parse_meloshort(data)
        elif "payload" in data and "url" in data["payload"]:  # vigloo format
            return JSONParser._parse_vigloo(data)
        elif "list" in data.get("data", {}):  # flikreels format
            return JSONParser._parse_flikreels(data)
        elif "episode_list" in data:  # freereels format
            return JSONParser._parse_freereels(data)
        elif "drama" in data and "episodes" in data:  # dramaflickreels format
            return JSONParser._parse_dramaflickreels(data)
        
        # Generic fallback - try common patterns
        return JSONParser._parse_generic(data)

    @staticmethod
    def universal_parse(data: Any) -> Dict[str, Any]:
        """
        Recursively search for video and subtitle URLs in any JSON structure.
        Returns combined data expected by bot.py
        """
        v_patterns = [
            r'\.m3u8(?:\?|$)', r'\.mp4(?:\?|$)', r'/hls/playlist\.m3u8',
            r'/bitly-stream/', r'stream_url', r'stream-url', r'm3u8-url'
        ]
        s_patterns = [r'\.vtt(?:\?|$)', r'\.srt(?:\?|$)', r'subtitle_url', r'sub_url']
        
        found_videos = []
        found_subtitles = []
        
        def _walk(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    k_lower = str(k).lower()
                    if isinstance(v, str) and v.startswith('http'):
                        is_v = any(re.search(p, v, re.I) for p in v_patterns) or \
                               any(sub in k_lower for sub in ['stream', 'm3u8', 'mp4', 'play_url', 'direct_link', 'video_url', 'sources'])
                        if is_v:
                            if v not in found_videos: found_videos.append(v)
                        
                        is_s = any(re.search(p, v, re.I) for p in s_patterns) or \
                               any(sub in k_lower for sub in ['subtitle', 'sub_url', 'zimu', 'sublist'])
                        if is_s:
                            if v not in found_subtitles: found_subtitles.append(v)
                    _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)
                    
        _walk(data)
        
        # Ekstrak semua episode terstruktur jika ada
        all_episodes = JSONParser.extract_all_episodes(data)
        
        # Tentukan default URL
        video_url = None
        if all_episodes:
            video_url = all_episodes[0].get("url")
        elif found_videos:
            video_url = found_videos[0]
            
        subtitle_url = None
        if all_episodes:
            subtitle_url = next((ep.get("subtitle_url") for ep in all_episodes if ep.get("subtitle_url")), None)
        elif found_subtitles:
            subtitle_url = found_subtitles[0]
            
        # Gabungkan hasil
        return {
            'videos': found_videos,
            'subtitles': found_subtitles,
            'all_episodes': all_episodes,
            'url': video_url,
            'subtitle_url': subtitle_url,
            'title': all_episodes[0].get("drama_title", "Video") if all_episodes else "Video",
            'cover_url': all_episodes[0].get("cover_url") if all_episodes else None,
            'source': all_episodes[0].get("source", "unknown") if all_episodes else "unknown"
        }
    
    @staticmethod
    def extract_all_episodes(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract all episodes from JSON data with subtitle detection
        Returns list of episodes with {title, url, subtitle_url, episode_num}
        """
        episodes = []
        
        try:
            # ── Dotdrama format ────────────────────────────────────────────────
            if "dgiv" in data and isinstance(data["dgiv"], dict) and "ebeer" in data["dgiv"]:
                dgiv = data["dgiv"]
                drama_info = dgiv.get("bswitc", {})
                drama_title = drama_info.get("nseri", "Drama")
                ebeer = dgiv.get("ebeer", [])
                logger.info(f"Detected dotdrama format with {len(ebeer)} episodes")
                for item in ebeer:
                    ep_num = str(item.get("ewheel", 1))
                    pphys = item.get("pphys", [])
                    video_url = None
                    if pphys:
                        video_url = pphys[0].get("Mopp") or pphys[0].get("Bcold")
                    
                    if video_url:
                        episodes.append({
                            "episode": ep_num,
                            "title": drama_title,
                            "url": video_url,
                            "subtitle_url": None,
                            "source": "dotdrama"
                        })
                if episodes:
                    episodes.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                    return episodes

            # ── Pocinca format ─────────────────────────────────────────────────
            if "series" in data and "videos" in data and isinstance(data["videos"], list):
                drama_title = data["series"].get("title", "Drama")
                videos = data["videos"]
                logger.info(f"Detected pocinca format with {len(videos)} episodes")
                for item in videos:
                    ep_num = str(item.get("index", 1))
                    video_url = item.get("main_url") or item.get("backup_url")
                    if video_url:
                        episodes.append({
                            "episode": ep_num,
                            "title": drama_title,
                            "url": video_url,
                            "subtitle_url": None,
                            "source": "pocinca"
                        })
                if episodes:
                    episodes.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                    return episodes

            # ── Velolo format ──────────────────────────────────────────────────
            if "videoInfo" in data and "episodesInfo" in data:
                drama_title = data["videoInfo"].get("name", "Video")
                rows = data["episodesInfo"].get("rows", [])
                logger.info(f"Detected velolo format with {len(rows)} episodes")
                for row in rows:
                    order = row.get("orderNumber", 0)
                    ep_num = str(order + 1)
                    video_url = row.get("videoAddress", "")
                    subtitle_url = row.get("zimu") or None
                    if video_url:
                        episodes.append({
                            "episode": ep_num,
                            "title": drama_title,
                            "url": video_url,
                            "subtitle_url": subtitle_url,
                            "has_subtitle": bool(subtitle_url),
                            "source": "velolo"
                        })
                if episodes:
                    episodes.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                    return episodes

            # ── Shortmax format ───────────────────────────────────────────────
            if ("shortPlayId" in data or "shortPlayName" in data or "episodes" in data) and ShortmaxParser:
                try:
                    parsed = ShortmaxParser.parse(data)
                    if parsed.get("episodes"):
                        logger.info(f"Detected shortmax format with {len(parsed['episodes'])} episodes")
                        return parsed["episodes"]
                except Exception as e:
                    logger.warning(f"ShortmaxParser error: {e}")

            # ── Netshort format ───────────────────────────────────────────────
            if "shortPlayEpisodeInfos" in data and NetshortParser:
                try:
                    parsed = NetshortParser.parse(data)
                    if parsed.get("episodes"):
                        logger.info(f"Detected netshort format with {len(parsed['episodes'])} episodes")
                        return parsed["episodes"]
                except Exception as e:
                    logger.warning(f"NetshortParser error: {e}")

            # ── FlickReels format ─────────────────────────────────────────────
            if ("drama" in data and data.get("drama", {}).get("source") == "dramaflickreels") or FlickReelsParser:
                try:
                    # FlickReelsParser.parse_json usually expects a file path, but we can adapt or check the structure
                    if "drama" in data and "episodes" in data:
                        drama = data["drama"]
                        drama_title = drama.get("title", "Video")
                        for ep in data["episodes"]:
                            raw = ep.get("raw", {})
                            video_url = raw.get("videoUrl") or ep.get("url")
                            if video_url:
                                sub_url = None
                                subs = raw.get("subtiles", [])
                                if isinstance(subs, list):
                                    for s in subs:
                                        if s.get("language") == "Indonesian":
                                            sub_url = s.get("url")
                                            break
                                episodes.append({
                                    "episode": str(ep.get("index", 0) + 1),
                                    "title": ep.get("name", drama_title),
                                    "drama_title": drama_title,
                                    "url": video_url,
                                    "subtitle_url": sub_url,
                                    "has_subtitle": bool(sub_url),
                                    "source": "dramaflickreels"
                                })
                        if episodes:
                            logger.info(f"Detected flickreels format with {len(episodes)} episodes")
                            return episodes
                except Exception as e:
                    logger.warning(f"FlickReels parsing error: {e}")

            # ── Vigloo format ───────────────────────────────────────────────
            if ("payloads" in data or "payload" in data) and ViglooParser:
                try:
                    parser = ViglooParser()
                    parsed = parser.parse(data)
                    if parsed.get("episodes"):
                        logger.info(f"Detected vigloo format with {len(parsed['episodes'])} episodes")
                        return parsed["episodes"]
                except Exception as e:
                    logger.warning(f"ViglooParser error: {e}")

            # ── Dramabox v2 format ───────────────────────────────────────────
            if "data" in data and isinstance(data["data"], dict) and "episodes" in data["data"] and isinstance(data["data"]["episodes"], list):
                book_data = data["data"]
                drama_title = book_data.get("bookName", "Drama")
                ep_list = book_data.get("episodes", [])
                logger.info(f"Detected dramabox v2 format with {len(ep_list)} episodes")
                for idx, item in enumerate(ep_list):
                    ep_num = str(item.get("chapterIndex", idx) + 1)
                    v_url = None
                    qualities = item.get("qualities", [])
                    if qualities:
                        for target in [1080, 720, 480]:
                            found = next((q["videoPath"] for q in qualities if q.get("quality") == target), None)
                            if found:
                                v_url = found
                                break
                        if not v_url: v_url = qualities[0].get("videoPath")
                    
                    if not v_url: v_url = item.get("url")

                    sub_url = None
                    subs = item.get("subtitles", [])
                    if subs:
                        indo = SubtitleDetector.find_indonesian_subtitle(subs)
                        if indo: sub_url = SubtitleDetector.get_subtitle_url(indo)

                    if v_url:
                        episodes.append({
                            "episode": ep_num,
                            "title": f"Episode {ep_num}",
                            "drama_title": drama_title,
                            "url": v_url,
                            "subtitle_url": sub_url,
                            "source": "dramabox_v2"
                        })
                if episodes:
                    episodes.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                    return episodes

            # ── Dramabox v1 format ───────────────────────────────────────────
            if "data" in data and isinstance(data["data"], dict) and "list" in data["data"]:
                items = data["data"]["list"]
                if items and isinstance(items, list) and any("cdn" in it or "multiVideos" in it for it in items if isinstance(it, dict)):
                    logger.info(f"Detected dramabox v1 format")
                    for idx, item in enumerate(items):
                        if not isinstance(item, dict): continue
                        ep_num = re.sub(r'[^0-9]', '', str(item.get("chapterName", idx+1))) or str(idx+1)
                        v_url = item.get("cdn")
                        if not v_url and item.get("multiVideos"):
                            v_url = item["multiVideos"][0].get("filePath")
                        
                        if v_url:
                            episodes.append({
                                "episode": ep_num,
                                "title": f"Episode {ep_num}",
                                "url": v_url,
                                "source": "dramabox_v1"
                            })
                    if episodes:
                        episodes.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                        return episodes

            # ── Flikreels format ─────────────────────────────────────────────
            if "data" in data and isinstance(data["data"], dict) and "list" in data["data"]:
                items = data["data"]["list"]
                if items and isinstance(items, list) and any("hls_url" in it for it in items if isinstance(it, dict)):
                    logger.info(f"Detected flikreels format")
                    for idx, item in enumerate(items):
                        if not isinstance(item, dict): continue
                        ep_num = str(item.get("chapter_num", idx + 1))
                        v_url = item.get("hls_url")
                        if v_url:
                            episodes.append({
                                "episode": ep_num,
                                "title": item.get("chapter_title", f"Episode {ep_num}"),
                                "url": v_url,
                                "source": "flikreels"
                            })
                    if episodes:
                        episodes.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                        return episodes

            # ── Dramawave / Freereels ────────────────────────────────────────
            if "episode_list" in data or ("data" in data and isinstance(data["data"], dict) and "info" in data["data"] and "episode_list" in data["data"]["info"]):
                ep_list = data.get("episode_list")
                drama_title = "Drama"
                if not ep_list:
                    info = data["data"]["info"]
                    ep_list = info.get("episode_list", [])
                    drama_title = info.get("name", "Drama")
                
                if isinstance(ep_list, list):
                    logger.info(f"Detected dramawave/freereels format with {len(ep_list)} episodes")
                    for idx, item in enumerate(ep_list):
                        ep_num = str(item.get("index", idx + 1))
                        v_url = item.get("external_audio_h264_m3u8") or item.get("video_url") or item.get("m3u8_url") or item.get("url")
                        sub_url = None
                        subs = item.get("subtitle_list")
                        if subs:
                            indo = SubtitleDetector.find_indonesian_subtitle(subs)
                            if indo: sub_url = SubtitleDetector.get_subtitle_url(indo)
                        
                        if v_url:
                            episodes.append({
                                "episode": ep_num,
                                "title": item.get("name", f"Episode {ep_num}"),
                                "drama_title": drama_title,
                                "url": v_url,
                                "subtitle_url": sub_url,
                                "source": "freereels"
                            })
                    if episodes:
                        episodes.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                        return episodes

            # ── Goodshort format ─────────────────────────────────────────────
            if "videos" in data and isinstance(data["videos"], list):
                logger.info(f"Detected goodshort format")
                for idx, item in enumerate(data["videos"]):
                    name = item.get("name", str(idx + 1))
                    ep_num = re.search(r'(\d+)', name).group(1) if re.search(r'(\d+)', name) else str(idx + 1)
                    v_url = item.get("url")
                    if v_url:
                        episodes.append({
                            "episode": ep_num,
                            "title": f"Episode {ep_num}",
                            "url": v_url,
                            "source": "goodshort"
                        })
                if episodes:
                    episodes.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                    return episodes

            # ── Stardust format ──────────────────────────────────────────────
            if "data" in data and isinstance(data["data"], dict) and "episodes" in data["data"] and isinstance(data["data"]["episodes"], dict):
                logger.info("Detected stardust format")
                for ep_num, ep_data in data["data"]["episodes"].items():
                    v_url = ep_data.get("h264") or ep_data.get("h265")
                    if v_url:
                        episodes.append({
                            "episode": str(ep_num),
                            "title": f"Episode {ep_num}",
                            "url": v_url,
                            "source": "stardust"
                        })
                if episodes:
                    episodes.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                    return episodes

            # ── Meloshort format ─────────────────────────────────────────────
            is_melo = ("drama_title" in data.get("data", {}) or "chapters" in data.get("data", {}))
            if is_melo:
                d = data["data"]
                drama_title = d.get("drama_title", "Drama")
                chapters = d.get("chapters", [d])
                if isinstance(chapters, list):
                    logger.info("Detected meloshort format")
                    for item in chapters:
                        ep_num = str(item.get("chapter_index", item.get("index", 1)))
                        v_url = item.get("play_url")
                        sub_url = None
                        if item.get("sublist"):
                            indo = SubtitleDetector.find_indonesian_subtitle(item["sublist"])
                            if indo: sub_url = SubtitleDetector.get_subtitle_url(indo)
                        
                        if v_url:
                            episodes.append({
                                "episode": ep_num,
                                "title": f"Episode {ep_num}",
                                "drama_title": drama_title,
                                "url": v_url,
                                "subtitle_url": sub_url,
                                "source": "meloshort"
                            })
                    if episodes:
                        episodes.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                        return episodes
            
            # Vigloo format
            elif "payload" in data and "url" in data["payload"]:
                payload = data["payload"]
                video_url = payload.get("url")
                cookies = payload.get("cookies", {})
                logger.info("Detected vigloo format with cookies")
                episodes.append({
                    "episode": "1",
                    "title": "Vigloo Video",
                    "url": video_url,
                    "cookies": cookies,
                    "source": "vigloo"
                })
            
            # Shortmax format
            elif ("videoUrl" in data or "shortPlayId" in data) and ShortmaxParser:
                parsed = ShortmaxParser.parse(data)
                return parsed["episodes"]
            
            # Netshort format
            elif "shortPlayEpisodeInfos" in data and NetshortParser:
                parsed = NetshortParser.parse(data)
                return parsed["episodes"]

            # Vigloo format
            elif ("payloads" in data or "payload" in data) and ViglooParser:
                logger.info("Detected vigloo format")
                parser = ViglooParser()
                parsed = parser.parse(data)
                return parsed["episodes"]

            # Goodshort format
            elif "videos" in data:
                for idx, item in enumerate(data["videos"]):
                    episode_num = item.get("name", str(idx + 1))
                    ep_match = re.search(r'(\d+)', episode_num)
                    if ep_match:
                        episode_num = ep_match.group(1)
                    
                    if "url" in item:
                        episodes.append({
                            "episode": episode_num,
                            "title": f"Episode {episode_num}",
                            "url": item["url"],
                            "subtitle_url": None
                        })
            
            # Flikreels format
            elif "data" in data and "list" in data["data"]:
                data_list = data["data"]["list"]
                if isinstance(data_list, list):
                    for item in data_list:
                        if not isinstance(item, dict): continue
                        if "hls_url" in item and item["hls_url"]:
                            episode_num = str(item.get("chapter_num", ""))
                        episodes.append({
                            "episode": episode_num,
                            "title": item.get("chapter_title", f"Episode {episode_num}"),
                            "url": item["hls_url"],
                            "subtitle_url": None
                        })
            
            # Freereels format with subtitle detection
            elif "episode_list" in data:
                ep_list = data["episode_list"]
                if isinstance(ep_list, list):
                    for idx, item in enumerate(ep_list):
                        if not isinstance(item, dict): continue
                        episode_num = str(item.get("index", idx + 1))
                    
                    if "external_audio_h264_m3u8" in item and item["external_audio_h264_m3u8"]:
                        video_url = item["external_audio_h264_m3u8"]
                    elif "external_audio_h265_m3u8" in item and item["external_audio_h265_m3u8"]:
                        video_url = item["external_audio_h265_m3u8"]
                    elif "video_url" in item and item["video_url"]:
                        video_url = item["video_url"]
                    elif "m3u8_url" in item and item["m3u8_url"]:
                        video_url = item["m3u8_url"]
                    
                    if not video_url:
                        # Fallback for empty url field in some formats
                        video_url = item.get("url") or item.get("video_url")
                    
                    subtitle_url = None
                    subs = item.get("subtitle_list")
                    if isinstance(subs, list) and len(subs) > 0:
                        indo_sub = SubtitleDetector.find_indonesian_subtitle(subs)
                        if indo_sub:
                            subtitle_url = SubtitleDetector.get_subtitle_url(indo_sub)
                    
                    if video_url:
                        episodes.append({
                            "episode": episode_num,
                            "title": item.get("name", f"Episode {episode_num}"),
                            "url": video_url,
                            "subtitle_url": subtitle_url
                        })
            
            # Dramaflickreels format
            elif "drama" in data and "episodes" in data:
                drama_info = data.get("drama", {})
                drama_name = drama_info.get("title", "Drama")
                ep_list = data.get("episodes", [])
                logger.info(f"Detected dramaflickreels format with {len(ep_list)} episodes")
                for ep_item in ep_list:
                    raw_data = ep_item.get("raw", {})
                    ep_num = str(raw_data.get("chapter_num", ep_item.get("index", 1)))
                    v_url = raw_data.get("videoUrl") or ep_item.get("url")
                    if v_url:
                        episodes.append({
                            "episode": ep_num,
                            "title": ep_item.get("name", f"Episode {ep_num}"),
                            "drama_title": drama_name,
                            "url": v_url,
                            "subtitle_url": None,
                            "has_subtitle": False,
                            "cover_url": drama_info.get("cover") or raw_data.get("chapter_cover")
                        })
            
            # Sort episodes by episode number
            episodes.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
            
        except Exception as e:
            logger.error(f"Error extracting all episodes: {e}")
        
        return episodes

    @staticmethod
    def extract_qualities_per_episode(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract kualitas video yang tersedia per episode dari JSON.

        Return list episode, masing-masing berisi field 'qualities':
          [
            {
              "episode": "1",
              "title": "Episode 1",
              "drama_title": "Drama Title",
              "subtitle_url": "...",
              "has_subtitle": True,
              "qualities": [
                {"label": "1080p", "url": "https://..."},
                {"label": "720p",  "url": "https://..."},
              ]
            },
            ...
          ]

        Format yang didukung:
          - dramabox_v2  → episode[].qualities[].quality + videoPath
          - dramabox_v1  → episode[].multiVideos[].type + filePath
          - dramawave    → episode[] dengan beberapa field URL berbeda resolusi
          - generic      → episode dengan satu URL saja (qualities = 1 item)
        """
        result = []

        try:
            # ── dramabox v2: episodes[].qualities[] ─────────────────────────
            data_dict = data.get("data")
            if (isinstance(data_dict, dict)
                    and "episodes" in data_dict
                    and isinstance(data_dict["episodes"], list)):

                book   = data_dict
                drama  = book.get("bookName", "Drama")
                for idx, ep in enumerate(book["episodes"]):
                    ep_num  = str(ep.get("chapterIndex", idx) + 1)
                    ep_title = ep.get("title", f"Episode {ep_num}")
                    raw_qs   = ep.get("qualities", [])

                    # Subtitle
                    sub_url = None
                    subs    = ep.get("subtitles", [])
                    if subs:
                        indo = SubtitleDetector.find_indonesian_subtitle(subs)
                        if indo:
                            sub_url = SubtitleDetector.get_subtitle_url(indo)

                    # Build quality list — sort descending by resolution number
                    qualities = []
                    for q in raw_qs:
                        label = str(q.get("quality", ""))
                        url   = q.get("videoPath", "")
                        if url:
                            # Normalize label: tambah "p" jika numeric
                            if label.isdigit():
                                label = f"{label}p"
                            qualities.append({"label": label, "url": url})

                    # Sort: 1080p > 720p > 480p > 360p
                    def _res_num(q):
                        try: return int(q["label"].rstrip("p"))
                        except: return 0
                    qualities.sort(key=_res_num, reverse=True)

                    if qualities:
                        result.append({
                            "episode":      ep_num,
                            "title":        ep_title,
                            "drama_title":  drama,
                            "subtitle_url": sub_url,
                            "has_subtitle": bool(sub_url),
                            "qualities":    qualities,
                        })

                result.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                if result:
                    return result

            # ── dramabox v1: list[].multiVideos[] ───────────────────────────
            if ("data" in data
                    and isinstance(data.get("data"), dict)
                    and "list" in data["data"]):

                for idx, item in enumerate(data["data"]["list"]):
                    ep_num = re.sub(r"[^0-9]", "", item.get("chapterName", str(idx+1))) or str(idx+1)
                    multi  = item.get("multiVideos", [])

                    qualities = []
                    if multi:
                        for v in multi:
                            label = v.get("type", "")
                            url   = v.get("filePath", "")
                            if url:
                                if label.isdigit():
                                    label = f"{label}p"
                                qualities.append({"label": label or "?", "url": url})
                    elif item.get("cdn"):
                        qualities = [{"label": "Default", "url": item["cdn"]}]

                    if qualities:
                        result.append({
                            "episode":      ep_num,
                            "title":        f"Episode {ep_num}",
                            "drama_title":  "Drama",
                            "subtitle_url": None,
                            "has_subtitle": False,
                            "qualities":    qualities,
                        })

                result.sort(key=lambda x: int(x["episode"]) if x["episode"].isdigit() else 0)
                if result:
                    return result

        except Exception as e:
            logger.error(f"extract_qualities_per_episode error: {e}")

        # ── Fallback: konversi extract_all_episodes → tiap ep punya 1 quality ─
        episodes = JSONParser.extract_all_episodes(data)
        for ep in episodes:
            ep["qualities"] = [{"label": "Default", "url": ep["url"]}]
        return episodes

    @staticmethod
    def _parse_goodshort(data: Dict) -> Tuple[Optional[str], Optional[str]]:
        """Parse goodshort JSON format"""
        try:
            if "videos" in data and len(data["videos"]) > 0:
                video = data["videos"][0]
                url = video.get("url")
                return url, None
        except Exception as e:
            logger.error(f"Error parsing goodshort: {e}")
        return None, None
    
    @staticmethod
    def _parse_dramabox(data: Dict) -> Tuple[Optional[str], Optional[str]]:
        """Parse dramabox JSON format (v1)"""
        try:
            if "data" in data and "list" in data["data"] and len(data["data"]["list"]) > 0:
                item = data["data"]["list"][0]
                if "cdn" in item and item["cdn"]:
                    return item["cdn"], None
                if "multiVideos" in item and len(item["multiVideos"]) > 0:
                    for vid in item["multiVideos"]:
                        if vid.get("type") == "720p" and vid.get("filePath"):
                            return vid["filePath"], None
                    first_vid = item["multiVideos"][0]
                    if first_vid.get("filePath"):
                        return first_vid["filePath"], None
        except Exception as e:
            logger.error(f"Error parsing dramabox: {e}")
        return None, None
    
    @staticmethod
    def _parse_dramabox_v2(data: Dict) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse dramabox v2 JSON format (dengan array episodes dan qualities)
        """
        try:
            if "data" in data and "episodes" in data["data"]:
                episodes = data["data"]["episodes"]
                if episodes and len(episodes) > 0:
                    # Ambil episode pertama
                    first_episode = episodes[0]
                    
                    # Cari video dengan kualitas terbaik
                    if "qualities" in first_episode and len(first_episode["qualities"]) > 0:
                        qualities = first_episode["qualities"]
                        
                        # Cari kualitas 1080p dulu, lalu 720p, lalu 540p
                        video_url = None
                        for quality in [1080, 720, 540, 480, 360]:
                            for q in qualities:
                                if q.get("quality") == quality and q.get("videoPath"):
                                    video_url = q.get("videoPath")
                                    logger.info(f"Found {quality}p video for dramabox v2")
                                    break
                            if video_url:
                                break
                        
                        # Jika tidak ketemu, ambil yang pertama
                        if not video_url and len(qualities) > 0:
                            video_url = qualities[0].get("videoPath")
                            logger.info(f"Using first quality available for dramabox v2")
                        
                        # Cari subtitle Indonesia
                        subtitle_url = None
                        if "subtitles" in first_episode and len(first_episode["subtitles"]) > 0:
                            indo_sub = SubtitleDetector.find_indonesian_subtitle(first_episode["subtitles"])
                            if indo_sub:
                                subtitle_url = SubtitleDetector.get_subtitle_url(indo_sub)
                                logger.info(f"Found Indonesian subtitle in dramabox v2: {subtitle_url}")
                        
                        return video_url, subtitle_url
                    
                    # Fallback ke URL langsung jika ada
                    elif "url" in first_episode:
                        return first_episode["url"], None
                        
        except Exception as e:
            logger.error(f"Error parsing dramabox v2: {e}")
        
        return None, None
    
    @staticmethod
    def _parse_dramawave(data: Dict) -> Tuple[Optional[str], Optional[str]]:
        """Parse dramawave JSON format with Indonesian subtitle detection"""
        try:
            if "data" in data and "info" in data["data"]:
                info = data["data"]["info"]
                if "episode_list" in info and len(info["episode_list"]) > 0:
                    episode = info["episode_list"][0]
                    for field in ["external_audio_h264_m3u8", "external_audio_h265_m3u8", "video_url", "m3u8_url"]:
                        if field in episode and episode[field]:
                            video_url = episode[field]
                            
                            subtitle_url = None
                            if "subtitle_list" in episode and len(episode["subtitle_list"]) > 0:
                                indo_sub = SubtitleDetector.find_indonesian_subtitle(episode["subtitle_list"])
                                if indo_sub:
                                    subtitle_url = SubtitleDetector.get_subtitle_url(indo_sub)
                                    logger.info(f"Found Indonesian subtitle in dramawave: {subtitle_url}")
                            
                            return video_url, subtitle_url
        except Exception as e:
            logger.error(f"Error parsing dramawave: {e}")
        return None, None
    
    @staticmethod
    def _parse_stardust(data: Dict) -> Tuple[Optional[str], Optional[str]]:
        """Parse stardust JSON format"""
        try:
            if "data" in data and "episodes" in data["data"]:
                episodes = data["data"]["episodes"]
                if episodes and "1" in episodes:
                    episode = episodes["1"]
                    if "h264" in episode and episode["h264"]:
                        return episode["h264"], None
                    if "h265" in episode and episode["h265"]:
                        return episode["h265"], None
        except Exception as e:
            logger.error(f"Error parsing stardust: {e}")
        return None, None
    
    @staticmethod
    def _parse_vigloo(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """Parse vigloo JSON format"""
        try:
            if ViglooParser:
                parser = ViglooParser()
                parsed = parser.parse(data)
                if parsed["episodes"]:
                    ep = parsed["episodes"][0]
                    return ep.get("url"), None
            
            if "payload" in data and "url" in data["payload"]:
                url = data["payload"]["url"]
                return url, None
        except Exception as e:
            logger.error(f"Error parsing vigloo: {e}")
        return None, None
    
    @staticmethod
    def _parse_meloshort(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """Parse meloshort JSON format with Indonesian subtitle detection"""
        try:
            if "data" in data:
                d = data["data"]
                video_url = d.get("play_url")
                
                subtitle_url = None
                if "sublist" in d and len(d["sublist"]) > 0:
                    indo_sub = SubtitleDetector.find_indonesian_subtitle(d["sublist"])
                    if indo_sub:
                        subtitle_url = SubtitleDetector.get_subtitle_url(indo_sub)
                        logger.info(f"Found Indonesian subtitle in meloshort: {subtitle_url}")
                
                if video_url:
                    return video_url, subtitle_url
        except Exception as e:
            logger.error(f"Error parsing meloshort: {e}")
        return None, None
    
    @staticmethod
    def _parse_flikreels(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """Parse flikreels JSON format"""
        try:
            if "data" in data and "list" in data["data"] and len(data["data"]["list"]) > 0:
                episode = data["data"]["list"][0]
                if "hls_url" in episode and episode["hls_url"]:
                    return episode["hls_url"], None
        except Exception as e:
            logger.error(f"Error parsing flikreels: {e}")
        return None, None
    
    @staticmethod
    def _parse_freereels(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """Parse freereels JSON format with Indonesian subtitle detection"""
        try:
            if "episode_list" in data and len(data["episode_list"]) > 0:
                episode = data["episode_list"][0]
                for field in ["external_audio_h264_m3u8", "external_audio_h265_m3u8", "video_url", "m3u8_url"]:
                    if field in episode and episode[field]:
                        video_url = episode[field]
                        
                        subtitle_url = None
                        if "subtitle_list" in episode and len(episode["subtitle_list"]) > 0:
                            indo_sub = SubtitleDetector.find_indonesian_subtitle(episode["subtitle_list"])
                            if indo_sub:
                                subtitle_url = SubtitleDetector.get_subtitle_url(indo_sub)
                                logger.info(f"Found Indonesian subtitle in freereels: {subtitle_url}")
                        
                        return video_url, subtitle_url
        except Exception as e:
            logger.error(f"Error parsing freereels: {e}")
        return None, None
    
    @staticmethod
    def _parse_velolo(data: Dict) -> Tuple[Optional[str], Optional[str]]:
        """Parse velolo JSON format (videoInfo + episodesInfo)"""
        try:
            rows = data.get("episodesInfo", {}).get("rows", [])
            if rows:
                first = rows[0]
                video_url = first.get("videoAddress")
                subtitle_url = first.get("zimu") or None
                if video_url:
                    logger.info(f"Velolo: video={video_url[:80]}, sub={subtitle_url}")
                    return video_url, subtitle_url
        except Exception as e:
            logger.error(f"Error parsing velolo: {e}")
        return None, None

    @staticmethod
    def _parse_dramaflickreels(data: Dict) -> Tuple[Optional[str], Optional[str]]:
        """Parse dramaflickreels JSON format"""
        try:
            episodes = data.get("episodes", [])
            if episodes:
                first = episodes[0]
                video_url = first.get("raw", {}).get("videoUrl") or first.get("url")
                if video_url:
                    return video_url, None
        except Exception as e:
            logger.error(f"Error parsing dramaflickreels: {e}")
        return None, None

    @staticmethod
    def _parse_generic(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """Generic fallback parser with subtitle detection"""
        video_url = None
        subtitle_url = None
        
        try:
            if isinstance(data, dict):
                url_fields = ["url", "video_url", "download_url", "cdn", "filePath", "play_url", "hls_url"]
                for field in url_fields:
                    val = data.get(field)
                    if isinstance(val, str):
                        video_url = val
                        break
                    elif isinstance(val, dict):
                        for nested_field in url_fields:
                            nested_val = val.get(nested_field)
                            if isinstance(nested_val, str):
                                video_url = nested_val
                                break
                
                # Look for subtitle in various places
                for sub_field in ["subtitles", "subtitle_list", "sublist", "subs"]:
                    subs_list = data.get(sub_field)
                    if isinstance(subs_list, list):
                        indo_sub = SubtitleDetector.find_indonesian_subtitle(subs_list)
                        if indo_sub:
                            subtitle_url = SubtitleDetector.get_subtitle_url(indo_sub)
                            break
                
                if not subtitle_url:
                    sub_fields = ["subtitle", "subtitle_url", "srt", "sub", "sub_file", "sub_link"]
                    for field in sub_fields:
                        val = data.get(field)
                        if isinstance(val, str):
                            if any(code in field.lower() for code in ["id", "ind", "bahasa", "indo"]):
                                subtitle_url = val
                                logger.info(f"Found possible Indonesian subtitle in field {field}")
                                break
                            elif not subtitle_url:
                                subtitle_url = val
                    
        except Exception as e:
            logger.error(f"Error in generic parser: {e}")
        
        return video_url, subtitle_url


class ProgressTracker:
    def __init__(self, total: int, callback=None):
        self.total = total
        self.current = 0
        self.callback = callback
        self.start_time: Optional[float] = None
        self.last_update: float = 0.0
        
    async def start(self):
        self.start_time = time.time()
        
    async def update(self, n: int):
        self.current = n
        now = time.time()
        
        if now - self.last_update > 0.5:
            self.last_update = now
            if self.callback:
                await self.callback(self.current, self.total)
    
    def get_speed(self) -> float:
        st = self.start_time
        if st is None:
            return 0.0
        elapsed = time.time() - st
        return self.current / elapsed if elapsed > 0 else 0.0


class RateLimiter:
    def __init__(self, max_concurrent: int):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.active = 0
        
    async def acquire(self):
        await self.semaphore.acquire()
        self.active += 1
        
    def release(self):
        self.semaphore.release()
        self.active -= 1
        
    async def __aenter__(self):
        await self.acquire()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()


async def cleanup_file(file_path: Path, delay: int = 0) -> bool:
    """
    Fungsi compatibility untuk cleanup file
    Args:
        file_path: Path file yang akan dihapus
        delay: Delay sebelum hapus
    Returns:
        bool: True jika berhasil
    """
    return await FileCleanup.safe_delete(file_path, delay)


def format_size(size_bytes: float) -> str:
    """Format file size"""
    if size_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes = size_bytes / 1024.0
    return f"{float(size_bytes):.1f} TB"


def format_speed(bytes_per_sec: float) -> str:
    """Format download/upload speed"""
    return f"{format_size(bytes_per_sec)}/s"