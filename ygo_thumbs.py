#!/usr/bin/env python3
"""
Download small/thumbnail Yu-Gi-Oh! card images from the YGOPRODeck API.

- Every card's main art AND alternate arts are downloaded.
- Each image is saved as <passcode>.jpg (e.g. 46986414.jpg).
- Already-downloaded images are skipped, so re-running only fetches new cards.
- Writes a manifest.json mapping each image passcode -> card info, which is
  handy for a deckbuilder (alt-art passcodes point back to the base card).
- Card names are then corrected against Yugipedia, since YGOPRODeck sometimes
  keeps fan-translated names. Yugipedia only lists each card's main passcode,
  so alt arts are corrected via their card_id.

Paths are resolved relative to this script's directory, so it can be run
from anywhere:
    <script dir>/card_images_small/<passcode>.jpg   images only
    <script dir>/manifest.json                      passcode -> card info
    <script dir>/failed.txt                         only written on failures

Standard library only (Python 3.8+). Usage:
    python ygo_thumbs.py                      # images -> card_images_small/
    python ygo_thumbs.py -w 8                 # 8 worker threads
    python ygo_thumbs.py --save-cardinfo      # also keep the raw API dump
"""

import argparse
import html
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

API_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
USER_AGENT = "ygo-thumb-downloader/1.0 (personal deckbuilder project)"

# YGOPRODeck limits clients to 20 requests/second and will temporarily ban
# anyone who exceeds it. Stay well under that.
MAX_REQUESTS_PER_SEC = 10

YUGIPEDIA_API = "https://yugipedia.com/api.php"
YUGIPEDIA_REQUESTS_PER_SEC = 1  # be polite; the whole pass is only ~30 requests
# Yugipedia's semantic search refuses offsets past 5000, so the card list is
# fetched in 10 chunks by the passcode's first digit (~1,500 cards each).
YUGIPEDIA_PAGE_SIZE = 500

SIZE_KEYS = {
    "small": "image_url_small",      # ~168x246 thumbnails
    "normal": "image_url",           # full card
    "cropped": "image_url_cropped",  # artwork only
}


class RateLimiter:
    """Simple thread-safe limiter: spaces requests evenly across all threads."""

    def __init__(self, per_second):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_time = time.monotonic()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.next_time > now:
                time.sleep(self.next_time - now)
                now = self.next_time
            self.next_time = now + self.interval


def http_get(url, limiter, retries=4, timeout=30):
    """GET a URL with rate limiting and exponential-backoff retries."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        limiter.wait()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise  # not worth retrying
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = e
        if attempt < retries:
            time.sleep(2 ** attempt)
    raise err


def fetch_card_data(limiter, cache_path=None):
    print("Fetching card list from YGOPRODeck...")
    raw = http_get(API_URL, limiter, timeout=120)
    data = json.loads(raw)["data"]
    msg = f"  {len(data)} cards received"
    if cache_path:
        with open(cache_path, "wb") as f:
            f.write(raw)
        msg += f" (saved full data to {cache_path})"
    print(msg)
    return data


def fetch_yugipedia_names(limiter):
    """Return {passcode: official English name} for every TCG/OCG card on Yugipedia."""
    print("Fetching official card names from Yugipedia...")
    names = {}
    digits = "0123456789"
    for i, digit in enumerate(digits, 1):
        offset = 0
        segment_count = 0
        while True:
            # Category:Duel Monsters cards excludes video-game pages, which
            # share passcodes with the real card (e.g. "Dark Magician (DM4)").
            query = (f"[[Category:Duel Monsters cards]][[Password::~{digit}*]]"
                     f"|?Password|?English name|limit={YUGIPEDIA_PAGE_SIZE}|offset={offset}")
            url = YUGIPEDIA_API + "?" + urllib.parse.urlencode(
                {"action": "ask", "format": "json", "api_version": "3", "query": query})
            resp = json.loads(http_get(url, limiter, timeout=60))
            if "error" in resp:
                raise RuntimeError(f"Yugipedia API error: {resp['error']}")
            results = resp.get("query", {}).get("results", [])
            segment_count += len(results)
            for row in results:
                (_, page), = row.items()
                printouts = page["printouts"]
                english = printouts.get("English name") or []
                if not english:
                    continue
                name = html.unescape(english[0])  # e.g. "Maliss <&#67;>" -> "Maliss <C>"
                for pw in printouts.get("Password") or []:
                    if pw.isdigit():
                        names[int(pw)] = name
            next_offset = resp.get("query-continue-offset")
            if not next_offset or next_offset <= offset:
                break
            offset = next_offset
        print(f"  [{i}/{len(digits)}] passcodes starting with {digit}: "
              f"{segment_count} cards (total so far {len(names)})", flush=True)
    print(f"  {len(names)} official names received")
    return names


def apply_official_names(manifest, official):
    """Overwrite manifest names with Yugipedia's, matching on the base card_id
    so alt arts get the same correction.

    If Yugipedia's name for a passcode is the name YGOPRODeck uses for a
    *different* passcode, YGOPRODeck has the cards on the wrong passcodes
    (e.g. it swaps Artmage Academic Arcane Arts Acropolis, printed 48739627,
    and Theorealize, printed 74733322). Yugipedia matches the printed cards,
    so its name wins, and that passcode's image must come from the other
    YGOPRODeck entry; those are returned as moves.

    Returns (changes, moves):
      changes: {card_id: (old name, new name)}
      moves:   {card_id: other card_id whose YGOPRODeck image is the right one}
    """
    owner = {}  # YGOPRODeck name -> base card_id, before any changes
    type_of = {}  # YGOPRODeck name -> card type, before any changes
    for pid, entry in manifest.items():
        if not entry["alt_art"]:
            owner.setdefault(entry["name"], int(pid))
            type_of.setdefault(entry["name"], entry.get("type"))

    changes, moves = {}, {}
    for entry in manifest.values():
        cid = entry["card_id"]
        new = official.get(cid)
        if not new or new == entry["name"]:
            continue
        changes.setdefault(cid, (entry["name"], new))
        other = owner.get(new)
        if other is not None and other != cid:
            moves[cid] = other
            # The card type belongs with the name, so it moves too.
            if type_of.get(new):
                entry["type"] = type_of[new]
        entry["name"] = new
    return changes, moves


def card_type(card):
    """"monster", "spell" or "trap" (which Neuron deck table the card goes in);
    None for skill cards and anything else that isn't played in a deck."""
    frame = (card.get("frameType") or "").lower()
    kind = (card.get("type") or "").lower()
    if frame == "spell" or kind.startswith("spell"):
        return "spell"
    if frame == "trap" or kind.startswith("trap"):
        return "trap"
    if frame in ("skill", "token") or kind in ("skill card", "token"):
        return None
    return "monster"


def build_jobs(cards, size_key):
    """Return (jobs, manifest). One job per unique image passcode."""
    jobs = {}
    manifest = {}
    for card in cards:
        base_id = card["id"]
        for img in card.get("card_images", []):
            pid = img.get("id")
            url = img.get(size_key)
            if pid is None or not url or pid in jobs:
                continue
            jobs[pid] = url
            manifest[str(pid)] = {
                "card_id": base_id,          # the card's main passcode
                "name": card.get("name"),
                "alt_art": pid != base_id,
            }
            kind = card_type(card)
            if kind:
                manifest[str(pid)]["type"] = kind  # Neuron deck table
    return jobs, manifest


def download_one(pid, url, out_dir, limiter, force=False):
    ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
    path = os.path.join(out_dir, f"{pid}{ext}")
    if not force and os.path.exists(path) and os.path.getsize(path) > 0:
        return "skipped", pid
    data = http_get(url, limiter)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)  # atomic, so an interrupted run never leaves a broken image
    return "downloaded", pid


def main():
    # Card names include characters like Ω and ・ that the default Windows
    # console encoding can't print.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-w", "--workers", type=int, default=4, help="download threads")
    ap.add_argument("--size", choices=SIZE_KEYS, default="small", help="image size")
    ap.add_argument("--save-cardinfo", action="store_true",
                    help="also save the raw API response as cardinfo.json next to the script")
    ap.add_argument("--skip-yugipedia", action="store_true",
                    help="keep YGOPRODeck's names instead of correcting them from Yugipedia")
    args = ap.parse_args()

    img_dir = os.path.join(SCRIPT_DIR, "card_images_small")
    manifest_path = os.path.join(SCRIPT_DIR, "manifest.json")
    os.makedirs(img_dir, exist_ok=True)
    limiter = RateLimiter(MAX_REQUESTS_PER_SEC)

    cache_path = os.path.join(SCRIPT_DIR, "cardinfo.json") if args.save_cardinfo else None
    cards = fetch_card_data(limiter, cache_path)
    jobs, manifest = build_jobs(cards, SIZE_KEYS[args.size])

    alt_count = sum(1 for m in manifest.values() if m["alt_art"])
    print(f"  {len(jobs)} images total ({alt_count} alternate arts)")

    force = set()  # passcodes whose image must be re-downloaded every run
    if not args.skip_yugipedia:
        try:
            official = fetch_yugipedia_names(RateLimiter(YUGIPEDIA_REQUESTS_PER_SEC))
        except Exception as e:
            print(f"  WARNING: Yugipedia name pass failed ({e}); keeping YGOPRODeck names.")
        else:
            changes, moves = apply_official_names(manifest, official)
            print(f"  {len(changes)} card names corrected:")
            for cid, (old, new) in sorted(changes.items(), key=lambda c: c[1][0]):
                print(f"    {cid}: {old!r} -> {new!r}")
            if moves:
                # Re-download these every run: a file saved before the fix (or by
                # --skip-yugipedia) would show the wrong card and otherwise be skipped.
                original_urls = dict(jobs)
                print(f"  {len(moves)} passcodes on the wrong card in YGOPRODeck; "
                      f"using the image from the correct entry:")
                for cid, src in sorted(moves.items()):
                    jobs[cid] = original_urls[src]
                    force.add(cid)
                    print(f"    {cid} ({manifest[str(cid)]['name']}): image from {src}")

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    counts = {"downloaded": 0, "skipped": 0, "failed": 0}
    failures = []
    total = len(jobs)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download_one, pid, url, img_dir, limiter, pid in force): pid
                   for pid, url in jobs.items()}
        for i, fut in enumerate(as_completed(futures), 1):
            pid = futures[fut]
            try:
                status, _ = fut.result()
                counts[status] += 1
            except Exception as e:
                counts["failed"] += 1
                failures.append((pid, str(e)))
            if i % 250 == 0 or i == total:
                print(f"  [{i}/{total}] downloaded={counts['downloaded']} "
                      f"skipped={counts['skipped']} failed={counts['failed']}")

    if failures:
        fail_path = os.path.join(SCRIPT_DIR, "failed.txt")
        with open(fail_path, "w") as f:
            for pid, err in failures:
                f.write(f"{pid}\t{jobs[pid]}\t{err}\n")
        print(f"{len(failures)} failures logged to {fail_path} - re-run to retry them.")

    print("Done.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
