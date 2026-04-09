import os
import uuid
import glob
import json
import threading
import yt_dlp
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)

# Use /tmp for Vercel serverless environment compatibility
DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}

def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    
    ydl_opts = {
        'outtmpl': os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s"),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
    }

    if format_choice == "audio":
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    elif format_id:
        ydl_opts['format'] = f"{format_id}+bestaudio/best"
        ydl_opts['merge_output_format'] = 'mp4'
    else:
        ydl_opts['format'] = 'bestvideo+bestaudio/best'
        ydl_opts['merge_output_format'] = 'mp4'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found. (FFmpeg might be missing on this server)"
            return

        # Prefer the target extension
        ext_target = ".mp3" if format_choice == "audio" else ".mp4"
        targets = [f for f in files if f.endswith(ext_target)]
        chosen = targets[0] if targets else files[0]

        # Cleanup other fragments
        for f in files:
            if f != chosen:
                try: os.remove(f)
                except: pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:30].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
            
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    ydl_opts = {
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Build quality options
            best_by_height = {}
            for f in info.get("formats", []):
                height = f.get("height")
                if height and f.get("vcodec") != "none":
                    tbr = f.get("tbr") or 0
                    if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                        best_by_height[height] = f

            formats = []
            for height, f in best_by_height.items():
                formats.append({
                    "id": f["format_id"],
                    "label": f"{height}p",
                    "height": height,
                })
            formats.sort(key=lambda x: x["height"], reverse=True)

            return jsonify({
                "title": info.get("title", ""),
                "thumbnail": info.get("thumbnail", ""),
                "duration": info.get("duration"),
                "uploader": info.get("uploader", ""),
                "formats": formats,
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    # Threading might not persist on Vercel after the response is sent
    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})

@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"status": "error", "error": "Job not found (Serverless context might have reset)"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })

@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready or expired"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    app.run(host="0.0.0.0", port=port, debug=True)
