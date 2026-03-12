import os
from pathlib import Path

# Bot Configuration
BOT_TOKEN = "8388093985:AAG1BrNL_bLsPIhab4uSqZheYofii62Ut5M"
ALLOWED_USERS = []  # Kosong = semua user diizinkan

# Pyrogram Configuration (MTProto Upload Cepat)
# Isi API_ID dan API_HASH dari my.telegram.org untuk mengaktifkan upload Pyrogram
# Jika dibiarkan 0 / "", bot akan menggunakan python-telegram-bot biasa (lambat)
API_ID = 30653860
API_HASH = "98e0a87077d4fc642ce183dfd7f46a19"

# Download Configuration
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Aria2 Configuration
ARIA2_HOST = "localhost"
ARIA2_PORT = 6800
ARIA2_SECRET = ""

# Upload Configuration
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
CHUNK_SIZE = 8 * 1024 * 1024  # 8MB chunks untuk upload lebih cepat

# Timeout Configuration (in seconds)
DOWNLOAD_TIMEOUT = 3600   # 1 hour per video
PROCESSING_TIMEOUT = 1800 # 30 minutes per video
UPLOAD_TIMEOUT = 1200     # 20 minutes per video
MESSAGE_TIMEOUT = 30      # 30 seconds for message updates

# ============================================================
# VIDEO ENCODING SETTINGS
# Target specs (dari MediaInfo output):
#   Format    : MPEG-4 / AVC (H.264)
#   Profile   : High@L3.1
#   Resolution: 720x1280 (portrait/vertical)
#   FPS       : 25.000 CFR
#   Bitrate   : 382 kbps video + 132 kbps audio = ~508 kbps
#   Codec     : x264, CABAC, 4 ref frames
#   Color     : YUV 4:2:0, 8-bit, BT.709 Limited
#   Audio     : AAC LC, stereo 2ch, 48kHz, 132kbps CBR
# ============================================================

# --- Resolution ---
TARGET_WIDTH        = 720
TARGET_HEIGHT       = 1280
TARGET_FPS          = 25
TARGET_FILE_SIZE_MB = 50           # Max < 50MB untuk Telegram

# --- Video codec ---
VIDEO_CODEC         = "libx264"
VIDEO_PROFILE       = "high"
VIDEO_LEVEL         = "3.1"
VIDEO_PRESET        = "medium"     # medium = balance quality/speed
VIDEO_TUNE          = "film"       # cocok untuk drama/series
PIX_FMT             = "yuv420p"    # YUV 4:2:0, 8-bit

# --- Video bitrate ---
TARGET_VIDEO_BITRATE = "382k"      # nominal ABR
TARGET_VIDEO_MAXRATE = "382k"
TARGET_VIDEO_BUFSIZE = "764k"      # 2x bitrate

# --- Color (BT.709 Limited) ---
COLOR_PRIMARIES = "bt709"
COLOR_TRC       = "bt709"
COLORSPACE      = "bt709"
COLOR_RANGE     = "tv"             # limited range

# --- x264 params (exact dari MediaInfo Encoding settings) ---
X264_PARAMS = (
    "cabac=1:ref=3:deblock=1:0:0:analyse=0x3:0x113:me=hex:subme=7:"
    "psy=1:psy_rd=1.00:0.00:mixed_ref=1:me_range=16:chroma_me=1:"
    "trellis=1:8x8dct=1:cqm=0:deadzone=21,11:fast_pskip=1:"
    "chroma_qp_offset=-2:bframes=3:b_pyramid=2:b_adapt=1:b_bias=0:"
    "direct=1:weightb=1:open_gop=0:weightp=2:keyint=250:keyint_min=25:"
    "scenecut=40:intra_refresh=0:rc_lookahead=40:rc=abr:mbtree=1:"
    "ratetol=1.0:qcomp=0.60:qpmin=0:qpmax=69:qpstep=4:ip_ratio=1.40:"
    "aq=1:1.00"
)

# --- Audio codec ---
AUDIO_CODEC          = "aac"
AUDIO_PROFILE        = "aac_low"   # AAC LC
TARGET_AUDIO_BITRATE = "132k"      # CBR dari MediaInfo
AUDIO_CHANNELS       = 2           # Stereo
AUDIO_SAMPLE_RATE    = 48000       # 48.0 kHz
AUDIO_CHANNEL_LAYOUT = "stereo"

# --- Container ---
OUTPUT_FORMAT = "mp4"
MOVFLAGS      = "+faststart"       # Web-optimized

# --- FFmpeg threads ---
FFMPEG_THREADS = 16

# --- Delete after upload ---
DELETE_AFTER_UPLOAD = True

# ============================================================
# AUTO CLEANUP
# ============================================================
CLEANUP_DELAY    = 5
CLEANUP_JSON     = True
CLEANUP_VIDEO    = True
CLEANUP_SUBTITLE = True
CLEANUP_OUTPUT   = True
CLEANUP_ON_ERROR = True

# ============================================================
# SESSION & CONCURRENCY
# ============================================================
SESSION_TIMEOUT          = 600
MAX_CONCURRENT_DOWNLOADS = 2  # Fokus untuk kecepatan 1-2 user
MAX_CONCURRENT_UPLOADS   = 2  # Menghindari bandwidth terbagi habis
MAX_RETRIES              = 3
RETRY_DELAY              = 5

# ============================================================
# SUPPORTED JSON SOURCES
# ============================================================
SUPPORTED_SOURCES = [
    "dramabox",
    "dramabox_v2",
    "dramawave",
    "flikreels",
    "goodshort",
    "freereels",
    "stardust",
    "vigloo",
    "meloshort",
    "velolo"
]

# ============================================================
# HLS DOWNLOADER
# ============================================================
MAX_SEGMENT_RETRIES       = 5
SEGMENT_CONCURRENCY       = 64  # Supercepat download chunks HLS
SEGMENT_TIMEOUT           = 20
PLAYLIST_REFRESH_INTERVAL = 30

# ============================================================
# COMPRESSION (fallback jika hasil > TARGET_FILE_SIZE_MB)
# ============================================================
ENABLE_AUTO_COMPRESS = True
MIN_BITRATE          = "200k"
MAX_BITRATE          = "382k"
COMPRESSION_ATTEMPTS = 3