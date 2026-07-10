"""Download Khmer font for calendar image generation."""
import os
import urllib.request
import logging

logger = logging.getLogger(__name__)

FONT_URL = "https://github.com/google/fonts/raw/main/ofl/notosanskhmer/NotoSansKhmer%5Bwdth%2Cwght%5D.ttf"
FONT_PATH = "/tmp/NotoSansKhmer.ttf"


def get_khmer_font():
    """Download Khmer font if not exists."""
    if os.path.exists(FONT_PATH):
        return FONT_PATH

    try:
        logger.info("Downloading Khmer font...")
        urllib.request.urlretrieve(FONT_URL, FONT_PATH)
        logger.info(f"Font saved to {FONT_PATH}")
        return FONT_PATH
    except Exception as e:
        logger.error(f"Font download failed: {e}")
        return None
