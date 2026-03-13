import json
from typing import Dict, Any, List, Optional, Tuple

class ShortmaxParser:
    @staticmethod
    def parse(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse Shortmax JSON format (supports both 'episodes' and 'shortPlayEpisodeInfos' structures).
        """
        result = {
            "title": data.get("shortPlayName") or data.get("title") or "Shortmax Video",
            "episodes": [],
            "cover": data.get("shortPlayCover") or data.get("cover"),
            "source": "shortmax"
        }
        
        # Structure 1: 'episodes' array (allepisode API)
        episodes_list = data.get("episodes", [])
        
        # Structure 2: 'shortPlayEpisodeInfos' array
        if not episodes_list:
            episodes_list = data.get("shortPlayEpisodeInfos", [])
            
        for ep in episodes_list:
            ep_num = str(ep.get("episodeNumber") or ep.get("episodeNo") or "")
            
            # Extract video URL
            video_url = None
            urls = ep.get("videoUrl", {})
            if isinstance(urls, dict):
                video_url = urls.get("video_1080") or urls.get("video_720") or urls.get("video_480")
            
            if not video_url:
                video_url = ep.get("playVoucher") or ep.get("url")
            
            if video_url:
                result["episodes"].append({
                    "episode": ep_num,
                    "title": result["title"],
                    "url": video_url,
                    "subtitle_url": None, 
                    "cover": ep.get("episodeCover") or ep.get("cover"),
                    "need_decrypt": ep.get("needDecrypt", False) or ep.get("need_decrypt", False)
                })
        
        return result
