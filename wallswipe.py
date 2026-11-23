#!/usr/bin/env python3
"""wallswipe: search several wallpaper sites at once, swipe yes/no, keep the yeses.

A stdlib-only local server. It fuses search results from the enabled sources into one
interleaved stream, serves the swipe UI, and downloads the original-resolution file of
every right-swipe into the configured folder. Everything swiped (either way) is
remembered, so it never shows up again.
"""

import argparse
import hashlib
import json
import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "wallswipe"
CONFIG_FILE = STATE_DIR / "config.json"
SEEN_FILE = STATE_DIR / "seen.json"
HISTORY_FILE = STATE_DIR / "history.json"
HISTORY_LIMIT = 100
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "wallswipe"
CACHE_LIMIT = 1 << 30  # bytes; oldest files go first once the image cache grows past this
UA = "wallswipe/1.0"
# The browser loads images through /img so the server can retry and cache them; only
# these hosts are proxied.
IMAGE_HOSTS = ("wallhaven.cc", "unsplash.com", "pexels.com")

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
history = load_json(HISTORY_FILE, [])  # newest first: {q, sort, ratio, minres, sources, cats}


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


    def details(self, wid):
        """Tags and other metadata the search listing leaves out (one API call per wallpaper)."""
        url = f"https://wallhaven.cc/api/v1/w/{wid}"
        if self.key:
            url += "?apikey=" + self.key
        d = http_json(url)["data"]
        return {"tags": [t["name"] for t in d["tags"]], "category": d["category"],
                "width": d["dimension_x"], "height": d["dimension_y"], "page": d["url"],
                "colors": d["colors"], "views": d["views"], "favorites": d["favorites"]}


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


EXTS = {"image/png": ".png", "image/webp": ".webp", "image/avif": ".avif", "image/jpeg": ".jpg"}
url_locks = {}  # one in-flight fetch per URL; a second request waits and hits the cache


def cache_path(url):
    return CACHE_DIR / hashlib.sha1(url.encode()).hexdigest()


def fetch_cached(url):
    """Fetch an image into the cache (with retries) and return (path, content type)."""
    host = urllib.parse.urlparse(url).hostname or ""
    if not host.endswith(IMAGE_HOSTS):
        raise ValueError(f"not an image host: {host}")
    path = cache_path(url)
    with lock:
        ulock = url_locks.setdefault(url, threading.Lock())
    with ulock:
        if path.exists():
            os.utime(path)
            return path, path.with_suffix(".type").read_text()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        last = None
        for attempt in range(4):
            if attempt:
                time.sleep(wait)
            wait = 2 ** attempt
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as r:
                    ctype = r.headers.get_content_type()
                    tmp = path.with_suffix(".part")
                    with open(tmp, "wb") as out:
                        shutil.copyfileobj(r, out)
                path.with_suffix(".type").write_text(ctype)
                tmp.replace(path)
                break
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code == 429:
                    wait = max(wait, int(e.headers.get("Retry-After") or 0))
                elif e.code < 500:
                    break  # 403/404 will not get better
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
            print(f"image fetch failed ({last}), attempt {attempt + 1}: {url}", flush=True)
        else:
            raise OSError(last)
        if not path.exists():
            raise OSError(last)
    trim_cache()
    return path, ctype


def trim_cache():
    files = sorted((p for p in CACHE_DIR.glob("*") if p.suffix == ""),
                   key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for p in files:
        if total <= CACHE_LIMIT:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        p.with_suffix(".type").unlink(missing_ok=True)


# --- swipe metadata and taste ----------------------------------------------------------

meta_queue = queue.Queue()
META_INTERVAL = 1.5  # seconds between detail calls; Wallhaven allows 45 per minute


def has_details(key):
    return "tags" in seen.get(key, {}).get("meta", {}) or seen.get(key, {}).get("meta", {}).get("gone")


def meta_worker():
    """Fill in tags for swiped wallpapers in the background, one API call at a time."""
    while True:
        key = meta_queue.get()
        source, wid = key.split(":", 1)
        src = SOURCES.get(source)
        with lock:
            skip = key not in seen or has_details(key)
        if skip or not hasattr(src, "details"):
            continue
        try:
            meta = src.details(wid)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(60)
                meta_queue.put(key)
                continue
            meta = {"gone": e.code}  # deleted upload; don't ask again
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            time.sleep(30)
            meta_queue.put(key)
            continue
        with lock:
            if key in seen:
                seen[key].setdefault("meta", {}).update(meta)
                save_json(SEEN_FILE, seen)
        time.sleep(META_INTERVAL)


def aspect_bucket(w, h):
    r = w / h
    for name, lo, hi in (("portrait", 0, 0.95), ("4:3-ish", 0.95, 1.5), ("16:10", 1.5, 1.7),
                         ("16:9", 1.7, 1.85), ("2:1", 1.85, 2.15), ("21:9", 2.15, 2.6),
                         ("ultrawide", 2.6, 99)):
        if lo <= r < hi:
            return name
    return "?"


def taste():
    """Which tags/categories/shapes come up more in right-swipes than in left-swipes.

    A tag's like rate is smoothed toward the overall rate (two pseudo-swipes), so a tag
    seen once doesn't top the list with 100%.
    """
    with lock:
        entries = [e for e in seen.values() if "tags" in e.get("meta", {})]
        pending = sum(1 for k in seen if not has_details(k) and k.startswith("wallhaven:"))
    yes = sum(e["verdict"] == "yes" for e in entries)
    base = (yes + 1) / (len(entries) + 2)

    def table(keyfn, min_seen):
        counts = defaultdict(lambda: [0, 0])
        for e in entries:
            for k in keyfn(e):
                counts[k][e["verdict"] != "yes"] += 1
        rows = [{"name": k, "yes": y, "no": n, "rate": (y + 2 * base) / (y + n + 2)}
                for k, (y, n) in counts.items() if y + n >= min_seen]
        return sorted(rows, key=lambda r: (-r["rate"], -r["yes"]))

    tags = table(lambda e: set(e["meta"]["tags"]), 2)
    return {
        "swipes": len(entries), "yes": yes, "base": base, "pending": pending,
        "liked": [r for r in tags if r["rate"] > base][:20],
        "disliked": [r for r in reversed(tags) if r["rate"] < base][:20],
        "categories": table(lambda e: [e["meta"].get("category", "?")], 1),
        "shapes": table(lambda e: [aspect_bucket(e["meta"]["width"], e["meta"]["height"])]
                        if e["meta"].get("width") else [], 1),
    }


def trim_bars(path):
    """Cut black letterbox/pillarbox bars that some uploads bake into the file.

    Wallpaper setters crop-to-fill, so baked-in bars end up on screen. Needs ImageMagick;
    without it the file is kept as is. Only symmetric, near-pure-black bars on two opposite
    sides count, so dark artwork isn't eaten into.
    """
    if not shutil.which("magick"):
        return
    def info(*args):
        return subprocess.run(["magick", str(path), *args, "info:"], capture_output=True,
                              text=True, timeout=60, check=True).stdout.split()
    try:
        w, h, c1, c2 = info("-format", "%w %h %[fx:p{0,0}.intensity] %[fx:p{w-1,h-1}.intensity]")
        tw, th, x, y = (int(v) for v in info("-fuzz", "2%", "-trim", "-format", "%w %h %X %Y"))
        w, h = int(w), int(h)
        dark = float(c1) < 0.03 and float(c2) < 0.03
        letterbox = tw == w and th < h * 0.97 and abs((h - th - y) - y) <= 4
        pillarbox = th == h and tw < w * 0.97 and abs((w - tw - x) - x) <= 4
        if not dark or not (letterbox or pillarbox):
            return
        tmp = path.with_name(path.stem + ".trim" + path.suffix)
        subprocess.run(["magick", str(path), "-crop", f"{tw}x{th}+{x}+{y}", "+repage",
                        "-quality", "95", str(tmp)], check=True, timeout=120)
        tmp.replace(path)
        print(f"trimmed bars: {path.name} {w}x{h} -> {tw}x{th}", flush=True)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print(f"trim failed for {path.name}: {e}", flush=True)


def download(item):
    """Save the full-resolution file; reuses the cached copy the card was shown from."""
    folder = Path(os.path.expanduser(config["folder"]))
    folder.mkdir(parents=True, exist_ok=True)
    cached, ctype = fetch_cached(item["full"])
    dest = folder / f"{item['source']}-{item['id']}{EXTS.get(ctype, '.jpg')}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    shutil.copyfile(cached, tmp)
    tmp.replace(dest)
    trim_bars(dest)
    src = SOURCES.get(item["source"])
    if hasattr(src, "on_download"):
        threading.Thread(target=src.on_download, args=(item,), daemon=True).start()
    return dest


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json", cache="no-store"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/img":
            src = urllib.parse.parse_qs(url.query).get("u", [""])[0]
            try:
                path, ctype = fetch_cached(src)
            except Exception as e:
                return self.send(502, {"error": str(e)})
            return self.send(200, path.read_bytes(), ctype, "private, max-age=86400")
        if url.path == "/api/config":
            with lock:
                stats = {"yes": sum(v["verdict"] == "yes" for v in seen.values()),
                         "no": sum(v["verdict"] == "no" for v in seen.values())}
            return self.send(200, {
                "folder": config["folder"], "stats": stats, "history": history,
                "sources": [{"name": s.name, "label": s.label, "available": s.available()}
                            for s in SOURCES.values()],
            })
        if url.path == "/api/taste":
            return self.send(200, taste())
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
            entry = {"verdict": verdict, "at": int(time.time()), "meta": {
                "width": item["width"], "height": item["height"], "page": item.get("page"),
                "title": item.get("title"), "author": item.get("author"),
                "colors": item.get("colors", []),
            }}
            if verdict == "yes":
                try:
                    entry["file"] = str(download(item))
                except Exception as e:
                    return self.send(502, {"error": f"download failed: {e}"})
            with lock:
                seen[key] = entry
                save_json(SEEN_FILE, seen)
            meta_queue.put(key)
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
        if url.path == "/api/history":
            # {"entry": {...}} records a search (moving a repeated query to the top);
            # {"delete": "query"} forgets one.
            with lock:
                q = data["entry"]["q"] if "entry" in data else data.get("delete")
                history[:] = [h for h in history if h["q"] != q]
                if "entry" in data:
                    history.insert(0, data["entry"])
                    del history[HISTORY_LIMIT:]
                save_json(HISTORY_FILE, history)
            return self.send(200, history)
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
    threading.Thread(target=meta_worker, daemon=True).start()
    with lock:
        for key in seen:  # backfill swipes made before metadata was kept
            if not has_details(key):
                meta_queue.put(key)

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
    if sys.argv[1:2] == ["--trim"]:  # one-off cleanup: wallswipe.py --trim FILE...
        for f in sys.argv[2:]:
            trim_bars(Path(f))
        sys.exit(0)
    main()
