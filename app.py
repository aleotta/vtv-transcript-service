"""
VTV Transcript Service v1.2.0
Fetches YouTube auto-generated transcripts via youtube-transcript-api.
Also fetches video metadata (title, description) via scraping youtube.com/watch.
Supports optional Webshare residential proxy to bypass YouTube IP blocks
on cloud providers (Render, AWS, etc.).
"""
import os
import re
import json
import requests
from flask import Flask, jsonify, request
from youtube_transcript_api import YouTubeTranscriptApi

try:
    from youtube_transcript_api.proxies import WebshareProxyConfig
    HAS_PROXY_SUPPORT = True
except ImportError:
    HAS_PROXY_SUPPORT = False

app = Flask(__name__)
SHARED_SECRET = os.environ.get("SHARED_SECRET", "")
WEBSHARE_USERNAME = os.environ.get("WEBSHARE_USERNAME", "")
WEBSHARE_PASSWORD = os.environ.get("WEBSHARE_PASSWORD", "")
USE_PROXY = bool(WEBSHARE_USERNAME and WEBSHARE_PASSWORD and HAS_PROXY_SUPPORT)


def _build_api() -> YouTubeTranscriptApi:
    if USE_PROXY:
        return YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=WEBSHARE_USERNAME,
                proxy_password=WEBSHARE_PASSWORD,
            )
        )
    return YouTubeTranscriptApi()


def _get_proxies_dict():
    if USE_PROXY:
        proxy_url = f"http://{WEBSHARE_USERNAME}:{WEBSHARE_PASSWORD}@p.webshare.io:80"
        return {"http": proxy_url, "https": proxy_url}
    return None


def _check_auth(req) -> bool:
    if not SHARED_SECRET:
        return True
    return req.headers.get("X-Auth-Token") == SHARED_SECRET


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "vtv-transcript",
        "version": "1.2.0",
        "proxy_enabled": USE_PROXY,
    })


@app.route("/metadata", methods=["GET"])
def metadata():
    if not _check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    video_id = (request.args.get("video_id") or "").strip()
    if not re.match(r"^[A-Za-z0-9_-]{11}$", video_id):
        return jsonify({"error": "invalid_video_id"}), 400
    url = f"https://www.youtube.com/watch?v={video_id}&hl=it"
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        r = requests.get(url, headers=headers, proxies=_get_proxies_dict(), timeout=30)
        if r.status_code != 200:
            return jsonify({"error": "fetch_failed", "status": r.status_code}), 500
        html = r.text
        # Try meta og:title
        title = ""
        m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        if m:
            title = m.group(1)
        # Try shortDescription from initial player data
        description = ""
        m = re.search(r'"shortDescription":"((?:[^"\\]|\\.)*)"', html)
        if m:
            raw = m.group(1)
            try:
                description = json.loads('"' + raw + '"')
            except Exception:
                description = raw.replace("\\n", "\n").replace('\\"', '"')
        # Fallback: meta og:description (truncated to ~160 char)
        if not description:
            m = re.search(r'<meta property="og:description" content="([^"]+)"', html)
            if m:
                description = m.group(1)
        # Channel name
        channel = ""
        m = re.search(r'"author":"([^"]+)"', html)
        if m:
            channel = m.group(1)
        return jsonify({
            "video_id": video_id,
            "title": title,
            "description": description,
            "channel": channel,
            "proxy_used": USE_PROXY,
        })
    except Exception as e:
        return jsonify({"error": "fetch_failed", "detail": str(e)[:300]}), 500


@app.route("/transcript", methods=["GET"])
def transcript():
    if not _check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    video_id = (request.args.get("video_id") or "").strip()
    if not re.match(r"^[A-Za-z0-9_-]{11}$", video_id):
        return jsonify({"error": "invalid_video_id"}), 400

    languages = (request.args.get("lang") or "it,en").split(",")
    languages = [l.strip() for l in languages if l.strip()]

    try:
        api = _build_api()
        try:
            fetched = api.fetch(video_id, languages=languages)
        except Exception:
            try:
                lst = api.list(video_id)
                first = next(iter(lst))
                fetched = first.fetch()
            except Exception as e2:
                return jsonify({"error": "no_transcript", "detail": str(e2)[:300]}), 404

        segments = []
        full_text = ""
        for s in fetched.snippets:
            start = float(s.start)
            text = (s.text or "").strip()
            if not text:
                continue
            hours = int(start // 3600)
            mins = int((start % 3600) // 60)
            secs = int(start % 60)
            ts = (
                f"{hours}:{mins:02d}:{secs:02d}"
                if hours > 0
                else f"{mins:02d}:{secs:02d}"
            )
            segments.append({"time": ts, "seconds": int(start), "text": text})
            full_text += f"[{ts}] {text}\n"

        return jsonify({
            "video_id": video_id,
            "language": getattr(fetched, "language_code", "unknown"),
            "segment_count": len(segments),
            "text": full_text,
            "segments": segments,
            "proxy_used": USE_PROXY,
        })

    except Exception as e:
        return jsonify({"error": "fetch_failed", "detail": str(e)[:300]}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
