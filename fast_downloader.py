import os
import json
import sys
import logging
import subprocess
import argparse
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional
from pathlib import Path

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class FastDownloader:
    def __init__(self, json_file: str, max_workers: int = 5):
        self.json_file = json_file
        self.max_workers = max_workers
        self.data = self._load_json()
        self.platform = self.detect_source()
        self.drama_name = self._get_drama_name()
        self.output_dir = Path(self.drama_name.replace(" ", "_"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _load_json(self) -> Dict[str, Any]:
        try:
            with open(self.json_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load JSON file: {e}")
            sys.exit(1)

    def _get_drama_name(self) -> str:
        data = self.data
        # Try to find a title in common locations
        name = None
        
        # Check specific platform fields
        if self.platform == "dotdrama":
            # dgiv -> ebeer -> pphys -> title?
            pass
        elif self.platform == "dramabox":
            name = data.get("data", {}).get("drama", {}).get("title")
        elif self.platform == "flikreels":
            name = data.get("data", {}).get("drama_title")
        elif self.platform == "vigloo":
            name = data.get("payload", {}).get("title")
            
        # Generic fallbacks
        if not name:
            name = data.get("title") or data.get("name") or data.get("drama_name") or data.get("shortPlayName")
            
        if not name and "data" in data and isinstance(data["data"], dict):
            name = data["data"].get("title") or data["data"].get("name") or data["data"].get("drama_name")

        return name if name else "Drama_Download"

    def detect_source(self) -> str:
        data = self.data
        if "dgiv" in data: return "dotdrama"
        
        # Check data.episodes variants
        if "data" in data and isinstance(data["data"], dict) and "episodes" in data["data"]:
            eps = data["data"]["episodes"]
            if isinstance(eps, list) and len(eps) > 0 and "h264" in eps[0]:
                return "stardust"
            return "dramabox"

        if "data" in data and isinstance(data["data"], dict) and "list" in data["data"]: return "flikreels"
        if "episode_list" in data: return "freereels"
        if "videos" in data and "url" in data["videos"]: return "goodshort"
        if "data" in data and isinstance(data["data"], dict) and "play_url" in data["data"]: return "meloshort"
        if "videos" in data and "main_url" in data["videos"]: return "pocinca"
        if "payload" in data and "url" in data["payload"]: return "vigloo"
        
        logger.error("Platform not detected from JSON structure.")
        sys.exit(1)

    def parse_dotdrama(self) -> List[str]:
        # dgiv -> ebeer -> pphys -> Mopp
        try:
            return [self.data["dgiv"]["ebeer"]["pphys"]["Mopp"]]
        except KeyError: return []

    def parse_dramabox(self) -> List[str]:
        # data -> episodes -> qualities -> videoPath
        urls = []
        try:
            episodes = self.data["data"]["episodes"]
            for ep in episodes:
                qualities = ep.get("qualities", [])
                if qualities:
                    urls.append(qualities[0].get("videoPath"))
        except KeyError: pass
        return [u for u in urls if u]

    def parse_flikreels(self) -> List[str]:
        # data -> list -> origin_down_url or hls_url
        urls = []
        try:
            items = self.data["data"]["list"]
            for item in items:
                url = item.get("origin_down_url") or item.get("hls_url")
                if url: urls.append(url)
        except KeyError: pass
        return urls

    def parse_freereels(self) -> List[str]:
        # episode_list -> external_audio_h264_m3u8
        urls = []
        try:
            items = self.data["episode_list"]
            for item in items:
                url = item.get("external_audio_h264_m3u8")
                if url: urls.append(url)
        except KeyError: pass
        return urls

    def parse_goodshort(self) -> List[str]:
        # videos -> url
        try:
            return [self.data["videos"]["url"]]
        except (KeyError, TypeError): return []

    def parse_meloshort(self) -> List[str]:
        # data -> play_url
        try:
            return [self.data["data"]["play_url"]]
        except (KeyError, TypeError): return []

    def parse_pocinca(self) -> List[str]:
        # videos -> main_url
        try:
            return [self.data["videos"]["main_url"]]
        except (KeyError, TypeError): return []

    def parse_stardust(self) -> List[str]:
        # data -> episodes -> h264
        urls = []
        try:
            items = self.data["data"]["episodes"]
            for item in items:
                url = item.get("h264")
                if url: urls.append(url)
        except KeyError: pass
        return urls

    def parse_vigloo(self) -> List[str]:
        # payload -> url
        try:
            return [self.data["payload"]["url"]]
        except (KeyError, TypeError): return []

    def download_video(self, url: str, ep_name: str):
        output_file = self.output_dir / f"{ep_name}.mp4"
        
        success = False
        for attempt in range(1, 4):
            try:
                logger.info(f"Downloading {ep_name} (Attempt {attempt})...")
                
                if ".m3u8" in url.lower():
                    # Use FFmpeg for m3u8
                    cmd = [
                        "ffmpeg", "-loglevel", "error", "-y",
                        "-i", url,
                        "-c", "copy",
                        str(output_file)
                    ]
                else:
                    # Use aria2c for mp4/direct files - ULTRA FAST settings
                    cmd = [
                        "aria2c", "-x", "16", "-s", "16", "-k", "1M",
                        "--max-connection-per-server=16",
                        "--retry-wait=5", "--max-tries=3",
                        "-o", f"{ep_name}.mp4",
                        "-d", str(self.output_dir),
                        "--console-log-level=warn",
                        url
                    ]
                    
                    # Add Vigloo cookies if available
                    if self.platform == "vigloo":
                        cookies = []
                        payload = self.data.get("payload", {})
                        for key in ["CloudFront-Policy", "CloudFront-Signature", "CloudFront-Key-Pair-Id"]:
                            val = payload.get(key)
                            if val: cookies.append(f"{key}={val}")
                        
                        if cookies:
                            cmd.insert(-1, f"--header=Cookie: {'; '.join(cookies)}")
                
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    logger.info(f"{ep_name} finished.")
                    success = True
                    break
                else:
                    logger.error(f"Error downloading {ep_name}: {result.stderr}")
            except Exception as e:
                logger.error(f"Exception during download of {ep_name}: {e}")
        
        if not success:
            logger.error(f"Failed to download {ep_name} after 3 attempts.")

    def run(self):
        parsers = {
            "dotdrama": self.parse_dotdrama,
            "dramabox": self.parse_dramabox,
            "flikreels": self.parse_flikreels,
            "freereels": self.parse_freereels,
            "goodshort": self.parse_goodshort,
            "meloshort": self.parse_meloshort,
            "pocinca": self.parse_pocinca,
            "stardust": self.parse_stardust,
            "vigloo": self.parse_vigloo,
        }
        
        parser_func = parsers.get(self.platform)
        if not parser_func:
            logger.error(f"No parser available for platform: {self.platform}")
            return

        urls = parser_func()
        if not urls:
            logger.warning(f"No video URLs found for platform {self.platform}.")
            return

        logger.info(f"Detected Platform: {self.platform}")
        logger.info(f"Found {len(urls)} episodes. Starting download...")

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            for i, url in enumerate(urls, 1):
                ep_name = f"EP_{i:03d}"
                executor.submit(self.download_video, url, ep_name)

        logger.info("Batch download process finished.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ultra Fast JSON Video Downloader")
    parser.add_argument("json_file", help="Path to the JSON file")
    parser.add_argument("--workers", type=int, default=5, help="Number of concurrent downloads")
    args = parser.parse_args()

    downloader = FastDownloader(args.json_file, max_workers=args.workers)
    downloader.run()
