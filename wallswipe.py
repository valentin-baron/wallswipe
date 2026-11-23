#!/usr/bin/env python3
"""wallswipe: search several wallpaper sites at once, swipe yes/no, keep the yeses.

A stdlib-only local server. It fuses search results from the enabled sources into one
interleaved stream, serves the swipe UI, and downloads the original-resolution file of
every right-swipe into the configured folder. Everything swiped (either way) is
remembered, so it never shows up again.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "wallswipe"
CONFIG_FILE = STATE_DIR / "config.json"
SEEN_FILE = STATE_DIR / "seen.json"
UA = "wallswipe/1.0"

# Aspect ratios offered in the UI, as width/height. Sources without a server-side
# ratio filter get filtered here with RATIO_TOLERANCE.
RATIOS = {"16x9": 16 / 9, "16x10": 16 / 10, "21x9": 21 / 9, "32x9": 32 / 9, "9x16": 9 / 16}
RATIO_TOLERANCE = 0.05

lock = threading.Lock()


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


config = {"folder": str(Path.home() / "Pictures/Wallpapers")} | load_json(CONFIG_FILE, {})
seen = load_json(SEEN_FILE, {})  # "source:id" -> {"verdict": "yes"|"no", "file": path?}


def key_from(env, secret_key):
    """API key from the environment, else from the `secret` keyring helper if present."""
    if os.environ.get(env):
        return os.environ[env]
    if shutil.which("secret") and not os.environ.get("CLAUDECODE"):
        try:
            out = subprocess.run(["secret", "get", secret_key], capture_output=True,
                                 text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def http_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA} | (headers or {}))
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def ratio_ok(w, h, ratio):
    if ratio in ("", "any"):
        return True
    if ratio == "landscape":
        return w > h
    if ratio == "portrait":
        return h > w
    return abs(w / h - RATIOS[ratio]) / RATIOS[ratio] <= RATIO_TOLERANCE


def orientation(ratio):
    if ratio in ("", "any"):
        return None
    return "portrait" if ratio in ("portrait", "9x16") else "landscape"


# --- sources -------------------------------------------------------------------------
# Each returns a list of normalized items:
#   {source, id, width, height, preview, full, page, author, title}
# `preview` is a mid-size image for the card, `full` the original-resolution file.

class Wallhaven:
    name = "wallhaven"
    label = "Wallhaven"

    def __init__(self):
        self.key = key_from("WALLHAVEN_API_KEY", "wallhaven.api_key")

    def available(self):
        return True

    def search(self, f, page):
        sorting = {"relevance": "relevance", "top": "toplist", "latest": "date_added",
                   "random": "random"}[f["sort"]]
        if not f["q"] and sorting == "relevance":
            sorting = "toplist"
        params = {
            "q": f["q"], "page": page, "sorting": sorting, "order": "desc",
            "categories": f.get("categories") or "111", "purity": "100",
        }
        if sorting == "toplist":
            params["topRange"] = f.get("range") or "1y"
        if sorting == "random":
            params["seed"] = f["seed"]
        if f["minres"]:
            params["atleast"] = f["minres"]
        if f["ratio"] not in ("", "any"):
            params["ratios"] = f["ratio"]
        if f.get("color"):
            params["colors"] = f["color"].lstrip("#")
        if self.key:
            params["apikey"] = self.key
        data = http_json("https://wallhaven.cc/api/v1/search?" + urllib.parse.urlencode(params))
        return [{
            "source": self.name, "id": d["id"],
            "width": d["dimension_x"], "height": d["dimension_y"],
            "thumb": d["thumbs"]["original"], "preview": d["path"], "full": d["path"],
            "page": d["url"], "author": None, "title": d["category"],
            "colors": d.get("colors", []),
        } for d in data["data"]]


class Unsplash:
    name = "unsplash"
    label = "Unsplash"

    def __init__(self):
        self.key = key_from("UNSPLASH_ACCESS_KEY", "unsplash.access_key")

    def available(self):
        return bool(self.key)

    def search(self, f, page):
        hdr = {"Authorization": f"Client-ID {self.key}", "Accept-Version": "v1"}
        params = {"page": page, "per_page": 30}
        orient = orientation(f["ratio"])
        if orient:
            params["orientation"] = orient
        if f["q"]:
            params["query"] = f["q"]
            params["order_by"] = "latest" if f["sort"] == "latest" else "relevant"
            if f.get("color_name"):
                params["color"] = f["color_name"]
            data = http_json("https://api.unsplash.com/search/photos?"
                             + urllib.parse.urlencode(params), hdr)["results"]
        else:
            params["order_by"] = "latest" if f["sort"] == "latest" else "popular"
            data = http_json("https://api.unsplash.com/photos?"
                             + urllib.parse.urlencode(params), hdr)
        return [{
            "source": self.name, "id": d["id"], "width": d["width"], "height": d["height"],
            "thumb": d["urls"]["small"], "preview": d["urls"]["regular"],
            "full": d["urls"]["raw"], "page": d["links"]["html"],
            "author": d["user"]["name"], "title": d.get("alt_description") or "",
            "download_location": d["links"]["download_location"],
        } for d in data]

    def on_download(self, item):
        # Required by the Unsplash API guidelines: count the download.
        loc = item.get("download_location")
        if loc:
            try:
                http_json(loc, {"Authorization": f"Client-ID {self.key}"})
            except (urllib.error.URLError, ValueError):
                pass


class Pexels:
    name = "pexels"
    label = "Pexels"

    def __init__(self):
        self.key = key_from("PEXELS_API_KEY", "pexels.api_key")

    def available(self):
        return bool(self.key)

    def search(self, f, page):
        hdr = {"Authorization": self.key}
        params = {"page": page, "per_page": 40}
        if f["q"]:
            params["query"] = f["q"]
            params["size"] = "large"
            orient = orientation(f["ratio"])
            if orient:
                params["orientation"] = orient
            if f.get("color_name"):
                params["color"] = f["color_name"]
            url = "https://api.pexels.com/v1/search?"
        else:
            url = "https://api.pexels.com/v1/curated?"
        data = http_json(url + urllib.parse.urlencode(params), hdr)["photos"]
        return [{
            "source": self.name, "id": str(d["id"]), "width": d["width"], "height": d["height"],
            "thumb": d["src"]["medium"], "preview": d["src"]["large2x"],
            "full": d["src"]["original"], "page": d["url"],
            "author": d["photographer"], "title": d.get("alt") or "",
        } for d in data]


SOURCES = {s.name: s for s in (Wallhaven(), Unsplash(), Pexels())}


def fused_search(f, page):
    """Query the chosen sources in parallel, filter, and interleave round-robin."""
    chosen = [SOURCES[n] for n in f["sources"] if n in SOURCES and SOURCES[n].available()]
    minw, minh = (map(int, f["minres"].split("x")) if f["minres"] else (0, 0))
    errors = {}

    def run(src):
        try:
            return src.search(f, page)
        except Exception as e:  # one dead source must not sink the others
            errors[src.name] = str(e)
            return []

    with ThreadPoolExecutor(len(chosen) or 1) as pool:
        lists = list(pool.map(run, chosen))

    with lock:
        filtered = [[i for i in lst
                     if f"{i['source']}:{i['id']}" not in seen
                     and i["width"] >= minw and i["height"] >= minh
                     and ratio_ok(i["width"], i["height"], f["ratio"])]
                    for lst in lists]
    if f["sort"] == "random":
        for lst in filtered:
            random.shuffle(lst)
    fused = []
    for row in range(max(map(len, filtered), default=0)):
        fused += [lst[row] for lst in filtered if row < len(lst)]
    # A source is exhausted once it returns nothing raw; the UI stops paging when all are.
    exhausted = all(len(lst) == 0 for lst in lists)
    return {"items": fused, "errors": errors, "exhausted": exhausted}


def download(item):
    folder = Path(os.path.expanduser(config["folder"]))
    folder.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(item["full"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        ctype = r.headers.get_content_type()
        ext = {"image/png": ".png", "image/webp": ".webp", "image/avif": ".avif"}.get(ctype, ".jpg")
        dest = folder / f"{item['source']}-{item['id']}{ext}"
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as out:
            shutil.copyfileobj(r, out)
        tmp.replace(dest)
    src = SOURCES.get(item["source"])
    if hasattr(src, "on_download"):
        threading.Thread(target=src.on_download, args=(item,), daemon=True).start()
    return dest


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/config":
            with lock:
                stats = {"yes": sum(v["verdict"] == "yes" for v in seen.values()),
                         "no": sum(v["verdict"] == "no" for v in seen.values())}
            return self.send(200, {
                "folder": config["folder"], "stats": stats,
                "sources": [{"name": s.name, "label": s.label, "available": s.available()}
                            for s in SOURCES.values()],
            })
        if url.path == "/api/search":
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            f = {
                "q": q.get("q", "").strip(), "sort": q.get("sort", "relevance"),
                "ratio": q.get("ratio", "any"), "minres": q.get("minres", ""),
                "sources": q.get("sources", "wallhaven").split(","),
                "categories": q.get("categories", "111"), "range": q.get("range", "1y"),
                "color": q.get("color", ""), "color_name": q.get("color_name", ""),
                "seed": q.get("seed", "abc123"),
            }
            return self.send(200, fused_search(f, int(q.get("page", 1))))
        self.send(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        data = self.body()
        if url.path == "/api/swipe":
            item, verdict = data["item"], data["verdict"]
            key = f"{item['source']}:{item['id']}"
            entry = {"verdict": verdict}
            if verdict == "yes":
                try:
                    entry["file"] = str(download(item))
                except Exception as e:
                    return self.send(502, {"error": f"download failed: {e}"})
            with lock:
                seen[key] = entry
                save_json(SEEN_FILE, seen)
            return self.send(200, entry)
        if url.path == "/api/undo":
            key = f"{data['source']}:{data['id']}"
            with lock:
                entry = seen.pop(key, None)
                save_json(SEEN_FILE, seen)
            if entry and entry.get("file"):
                Path(entry["file"]).unlink(missing_ok=True)
            return self.send(200, {"undone": bool(entry)})
        if url.path == "/api/config":
            if data.get("folder"):
                config["folder"] = data["folder"]
                save_json(CONFIG_FILE, config)
            return self.send(200, config)
        if url.path == "/api/reset-seen":
            with lock:
                n = sum(1 for v in seen.values() if v["verdict"] == "no")
                for k in [k for k, v in seen.items() if v["verdict"] == "no"]:
                    del seen[k]
                save_json(SEEN_FILE, seen)
            return self.send(200, {"cleared": n})
        self.send(404, {"error": "not found"})


def open_window(url):
    """Prefer a chromeless app window; fall back to the default browser."""
    for browser in ("chromium", "google-chrome-stable", "brave", "microsoft-edge-stable"):
        if shutil.which(browser):
            profile = STATE_DIR / "browser"
            return subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}",
                                     "--class=wallswipe", "--no-first-run",
                                     "--no-default-browser-check"])
    webbrowser.open(url)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=0, help="default: any free port")
    ap.add_argument("--no-window", action="store_true", help="only serve, print the URL")
    args = ap.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"wallswipe on {url}  (saving to {config['folder']})", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    proc = None if args.no_window else open_window(url)
    try:
        if proc:
            proc.wait()  # own browser profile, so this blocks until the window closes
        else:
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    server.shutdown()
    sys.exit(0)


if __name__ == "__main__":
    main()
