#!/usr/bin/env bash
# Install system dependencies for WeasyPrint
apt-get update
apt-get install -y \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    fonts-noto \
    fonts-noto-cjk \
    fonts-noto-color-emoji

# Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt
