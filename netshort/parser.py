import json
from typing import Dict, Any, List, Optional

class NetshortParser:
    @staticmethod
    def parse(data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse Netshort JSON format.
        Based on the provided sample with 'shortPlayEpisodeInfos'.
        """
        result = {
            "title": data.get("shortPlayName") or data.get("title") or "Netshort Video",
            "episodes": [],
            "cover": data.get("shortPlayCover") or data.get("cover"),
            "source": "netshort"
        }
        
        episodes_list = data.get("shortPlayEpisodeInfos", [])
        if not episodes_list:
            # Fallback for other potential structures
            episodes_list = data.get("episodes", [])
            
        for ep in episodes_list:
            ep_num = str(ep.get("episodeNo") or ep.get("episodeNumber") or "")
            
            # Extract video URL
            video_url = ep.get("playVoucher") or ep.get("url")
            
            # Check for clarity-specific URLs if they exist in some variants
            urls = ep.get("videoUrl", {})
            if isinstance(urls, dict) and not video_url:
                video_url = urls.get("video_1080") or urls.get("video_720") or urls.get("video_480")
            
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
