import os
import threading
import time
import requests
import yt_dlp
from flask import Flask, render_template, request, jsonify
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, TDRC, TXXX, APIC

app = Flask(__name__)

LIDARR_URL = os.getenv("LIDARR_URL", "http://127.0.0.1:8686")
LIDARR_API_KEY = os.getenv("LIDARR_API_KEY", "")
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "/DATA/Downloads")

download_status = {}

def get_headers():
    return {"X-Api-Key": LIDARR_API_KEY}

def get_missing_albums():
    try:
        endpoint = f"{LIDARR_URL}/api/v1/wanted/missing"
        params = {"apikey": LIDARR_API_KEY, "sortKey": "releaseDate", "sortDir": "desc", "pageSize": 200}
        response = requests.get(endpoint, params=params)
        response.raise_for_status()
        records = response.json().get('records', [])
        
        for item in records:
            cover_url = ""
            if 'images' in item:
                for img in item['images']:
                    if img['coverType'] == 'Cover':
                        cover_url = img['url']
                        break
            item['coverUrl'] = cover_url
            
        return records
    except Exception as e:
        print(f"Error fetching albums: {e}")
        return []

def trigger_manual_import_scan(folder_path):
    try:
        endpoint = f"{LIDARR_URL}/api/v1/command"
        payload = {
            "name": "DownloadedAlbumsScan",
            "path": folder_path,
            "importMode": "Auto"
        }
        requests.post(endpoint, json=payload, headers=get_headers())
    except Exception as e:
        print(f"Failed to trigger import scan: {e}")

def download_image_data(remote_url):
    if not remote_url:
        return None
    try:
        full_url = f"{LIDARR_URL}{remote_url}"
        resp = requests.get(full_url, params={'apikey': LIDARR_API_KEY}, stream=True)
        if resp.status_code == 200:
            return resp.content
    except Exception:
        pass
    return None

def tag_file_with_metadata(file_path, title, artist, album, track_num, year, mb_release_id, mb_artist_id, cover_data):
    try:
        audio = ID3(file_path)
    except Exception:
        audio = ID3()
    
    audio.add(TIT2(encoding=3, text=title))
    audio.add(TPE1(encoding=3, text=artist))
    audio.add(TALB(encoding=3, text=album))
    audio.add(TRCK(encoding=3, text=str(track_num)))
    if year:
        audio.add(TDRC(encoding=3, text=str(year)))

    if mb_release_id:
        audio.add(TXXX(encoding=3, desc='MusicBrainz Album Id', text=mb_release_id))
        audio.add(TXXX(encoding=3, desc='MusicBrainz Album Type', text='album'))
    
    if mb_artist_id:
        audio.add(TXXX(encoding=3, desc='MusicBrainz Artist Id', text=mb_artist_id))

    if cover_data:
        audio.add(APIC(
            encoding=3,
            mime='image/jpeg',
            type=3,
            desc='Cover',
            data=cover_data
        ))

    audio.save(file_path, v2_version=3)

def download_album_task(album_id, album_title, artist_name, artist_id, mb_release_id, cover_url):
    global download_status
    
    def format_speed(speed_bytes):
        if not speed_bytes: return "0 B/s"
        if speed_bytes > 1024 * 1024:
            return f"{speed_bytes / (1024 * 1024):.2f} MB/s"
        if speed_bytes > 1024:
            return f"{speed_bytes / 1024:.1f} KB/s"
        return f"{speed_bytes} B/s"

    def progress_hook(d):
        if d['status'] == 'downloading':
            speed = d.get('speed')
            if speed and album_id in download_status:
                download_status[album_id]['speed_str'] = format_speed(speed)
        elif d['status'] == 'finished':
            if album_id in download_status:
                download_status[album_id]['speed_str'] = ""

    download_status[album_id] = {'state': 'preparing', 'current': 0, 'total': 0, 'percent': 0, 'speed_str': ''}
    
    try:
        tracks_resp = requests.get(f"{LIDARR_URL}/api/v1/track?albumId={album_id}", headers=get_headers())
        tracks = tracks_resp.json()
        valid_tracks = [t for t in tracks if t.get('title')]
        
        total_tracks = len(valid_tracks)
        if total_tracks == 0:
            raise Exception("No valid tracks found")
            
        download_status[album_id]['state'] = 'downloading'

        safe_artist = "".join([c for c in artist_name if c.isalnum() or c in (' ', '-', '_')]).strip()
        safe_album = "".join([c for c in album_title if c.isalnum() or c in (' ', '-', '_')]).strip()
        
        album_folder_path = os.path.join(DOWNLOAD_PATH, safe_artist, safe_album)
        if not os.path.exists(album_folder_path):
            os.makedirs(album_folder_path)

        cover_data = download_image_data(cover_url)
        count = 0
        
        for track in valid_tracks:
            track_title = track.get('title')
            track_number = track.get('trackNumber', 0)
            
            filename_no_ext = f"{int(track_number):02d} - {track_title}"
            filename_no_ext = "".join([c for c in filename_no_ext if c.isalnum() or c in (' ', '-', '_')]).strip()
            
            file_template_path = os.path.join(album_folder_path, f"{filename_no_ext}.%(ext)s")
            file_final_path = os.path.join(album_folder_path, f"{filename_no_ext}.mp3")

            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': file_template_path,
                'postprocessors': [
                    {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                ],
                'progress_hooks': [progress_hook],
                'quiet': True,
                'no_warnings': True,
                'nocheckcertificate': True,
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                }
            }

            search_query = f"ytsearch1:{artist_name} {track_title} audio"
            
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([search_query])
                
                if os.path.exists(file_final_path):
                    tag_file_with_metadata(
                        file_path=file_final_path,
                        title=track_title,
                        artist=artist_name,
                        album=album_title,
                        track_num=track_number,
                        year="",
                        mb_release_id=mb_release_id,
                        mb_artist_id=artist_id,
                        cover_data=cover_data
                    )

            except Exception as e:
                print(f"Track download error: {e}")
            
            count += 1
            percent = int((count / total_tracks) * 100) if total_tracks > 0 else 0
            download_status[album_id].update({
                'state': 'downloading',
                'current': count,
                'total': total_tracks,
                'percent': percent
            })

        download_status[album_id]['state'] = 'importing'
        download_status[album_id]['speed_str'] = ''
        trigger_manual_import_scan(album_folder_path)
        
        import_success = False
        for _ in range(12):
            time.sleep(5)
            try:
                resp = requests.get(f"{LIDARR_URL}/api/v1/trackfile?albumId={album_id}", headers=get_headers())
                if resp.status_code == 200:
                    imported_files = resp.json()
                    if len(imported_files) >= total_tracks:
                        import_success = True
                        break
            except Exception:
                pass

        if import_success:
            download_status[album_id]['state'] = 'imported'
        else:
            download_status[album_id]['state'] = 'import_failed'

    except Exception as e:
        print(f"Album download error: {e}")
        download_status[album_id]['state'] = 'error'

@app.route('/')
def index():
    raw_albums = get_missing_albums()
    albums = []
    for item in raw_albums:
        albums.append({
            'id': item['id'],
            'title': item['title'],
            'artist': item['artist']['artistName'],
            'artistId': item['artist']['foreignArtistId'],
            'mbId': item.get('foreignReleaseId', ''), 
            'year': str(item.get('releaseDate', ''))[:4],
            'coverUrl': item.get('coverUrl', '')
        })
    
    albums.sort(key=lambda x: x['artist'])
    
    return render_template('index.html', albums=albums)

@app.route('/start_download', methods=['POST'])
def start_download():
    data = request.json
    album_id = data.get('id')
    
    if album_id in download_status and download_status[album_id]['state'] == 'downloading':
        return jsonify({"status": "already_downloading"})

    thread = threading.Thread(target=download_album_task, args=(
        album_id, 
        data.get('title'), 
        data.get('artist'), 
        data.get('artistId'),
        data.get('mbId'),
        data.get('coverUrl')
    ))
    thread.start()
    return jsonify({"status": "started"})

@app.route('/status/<int:album_id>')
def status(album_id):
    stat = download_status.get(album_id, {'state': 'idle', 'percent': 0, 'speed_str': ''})
    return jsonify(stat)

if __name__ == '__main__':
    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)
    app.run(host='0.0.0.0', port=5000)
