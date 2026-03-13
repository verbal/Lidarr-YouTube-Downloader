<div align="center">

# 🎵 Lidarr YouTube Downloader

![Version](https://img.shields.io/badge/version-1.5.2-blue.svg?style=for-the-badge)
![Python Alpine](https://img.shields.io/badge/python-alpine-yellow.svg?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/docker-ready-blue.svg?style=for-the-badge&logo=docker&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green.svg?style=for-the-badge)

**Bridge the gap between Lidarr and YouTube**

Automatically download missing albums with perfect metadata tagging

</div>

---

## ✨ Features

- 🔍 **Auto-finds** high-quality audio on YouTube (up to 320kbps)
- 🏷️ **Smart tagging** with MusicBrainz + iTunes metadata
- 📦 **Direct import** to your Lidarr library
- 🌓 **Modern UI** with dark/light mode
- 🎚️ **Configurable filters** - customize forbidden words
- 🔔 **Telegram and Discord notifications**
- 🐳 **Docker ready** - deploy in seconds

---

## 🚀 Quick Start

### Docker Compose

```yaml
services:
  lidarr-downloader:
    image: angrido/lidarr-downloader:latest
    container_name: lidarr-downloader
    ports:
      - "5005:5000"
    volumes:
      - /DATA/Downloads:/DATA/Downloads
      - /DATA/Media/Music:/music
    environment:
      - LIDARR_URL=http://192.168.1.XXX:8686
      - LIDARR_API_KEY=your_api_key_here
      - DOWNLOAD_PATH=/DATA/Downloads
      - LIDARR_PATH=/music
      - PUID=1000
      - PGID=1000
      - UMASK=002
    restart: unless-stopped
```

**Access**: `http://localhost:5005`

---

## ⚙️ Configuration

### Required Settings

| Variable | Example | Description |
|----------|---------|-------------|
| `LIDARR_URL` | `http://192.168.1.10:8686` | Lidarr address (use IP) |
| `LIDARR_API_KEY` | `abc123...` | From Lidarr → Settings → General |
| `DOWNLOAD_PATH` | `/DATA/Downloads` | Download folder |

### Optional Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `LIDARR_PATH` | - | Final library path (optional) |
| `SCHEDULER_ENABLED` | `false` | Auto-check missing albums |
| `SCHEDULER_INTERVAL` | `60` | Check interval (minutes) |

> 💡 **All settings configurable via Web UI!**


## 📸 Screenshots

<p align="center">
  <img src="https://github.com/user-attachments/assets/3feaa81a-0f2a-4bb4-8130-f721388118b6" width="45%">
  <img src="https://github.com/user-attachments/assets/279647b8-8dca-4273-aaaf-d7dfce12b268" width="45%">
</p>

---

## 🔄 Upgrading from JSON to SQLite

If upgrading from a version that used JSON files (`download_history.json`, `download_logs.json`, `last_failed_result.json`), run the migration tool:

```bash
# Inside the container:
python3 tools/migrate_json_to_db.py --config-dir /config

# Or from the host if config is mounted:
python3 tools/migrate_json_to_db.py --config-dir ./config
```

This imports data into the SQLite database and renames the originals to `*.json.migrated`.

---

## ⚠️ Disclaimer

This tool is for **educational purposes** and managing your personal library.  
Users are responsible for complying with copyright laws and YouTube's ToS.

---

<div align="center">

**Made with ❤️**


</div>
