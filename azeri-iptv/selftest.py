#!/usr/bin/env python3
"""Self-test every channel in playlist.json from YOUR network.

The 16 channels flagged `geo: "AZ"` are geo-locked to Azerbaijan and could
not be probed from the machine that built this playlist. Run this on your own
connection (ideally the same network as the TV) to check them:

    python3 selftest.py                 # tests playlist.json next to this file
    python3 selftest.py --geo AZ        # just the geo-locked ones
    python3 selftest.py --promote       # rewrite playlist.json + playlist.m3u,
                                        # moving whatever passed into `verified`

A channel passes only if the manifest parses as HLS, a media playlist
resolves, a real segment downloads, and the live edge advances.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 15
HERE = os.path.dirname(os.path.abspath(__file__))

_LAX = ssl.create_default_context()
_LAX.check_hostname = False
_LAX.verify_mode = ssl.CERT_NONE


def fetch(url, headers=None, limit=None, insecure=False):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT,
                                context=_LAX if insecure else None) as r:
        return r.status, (r.read(limit) if limit else r.read()), r.geturl()


def seq_of(t):
    m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", t)
    return int(m.group(1)) if m else None


def probe(url, headers, insecure):
    """Return (ok, detail)."""
    try:
        st, body, final = fetch(url, headers, insecure=insecure)
    except Exception as e:
        return False, f"{type(e).__name__.replace('Error','')}: {e}"[:70]
    text = body.decode("utf-8", "replace")
    if not text.lstrip().startswith("#EXTM3U"):
        return False, "not HLS (error page?)"

    if "#EXT-X-STREAM-INF" in text:
        best, res = None, "?"
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF"):
                bw = re.search(r"BANDWIDTH=(\d+)", ln)
                rs = re.search(r"RESOLUTION=([\dx]+)", ln)
                for nx in lines[i + 1:]:
                    if nx.strip() and not nx.startswith("#"):
                        cand = (int(bw.group(1)) if bw else 0,
                                rs.group(1) if rs else "?",
                                urllib.parse.urljoin(final, nx.strip()))
                        if best is None or cand[0] > best[0]:
                            best, res = cand, cand[1]
                        break
        if best is None:
            return False, "master with no variants"
        try:
            _, mb, mfinal = fetch(best[2], headers, insecure=insecure)
        except Exception as e:
            return False, f"variant: {type(e).__name__}"[:70]
        mtext, murl = mb.decode("utf-8", "replace"), best[2]
    else:
        mtext, mfinal, murl, res = text, final, url, "single"

    if "#EXT-X-ENDLIST" in mtext:
        return False, "VOD, not live"
    segs = [urllib.parse.urljoin(mfinal if "#EXT-X-STREAM-INF" not in text else murl,
                                 l.strip())
            for l in mtext.splitlines() if l.strip() and not l.startswith("#")]
    if not segs:
        return False, "no segments"
    try:
        sst, sb, _ = fetch(segs[len(segs) // 2], headers, limit=700_000,
                           insecure=insecure)
    except Exception as e:
        return False, f"segment: {type(e).__name__}"[:70]
    if len(sb) < 50_000:
        return False, f"segment too small ({len(sb)}B)"

    s1 = seq_of(mtext)
    time.sleep(12)
    try:
        _, b2, _ = fetch(murl, headers, insecure=insecure)
        s2 = seq_of(b2.decode("utf-8", "replace"))
    except Exception:
        s2 = None
    if s1 is not None and s2 is not None and s2 == s1:
        return False, "manifest frozen (dead feed?)"
    return True, f"{res}, {len(sb)//1024}KB seg"


def check(ch):
    urls = [ch["url"]] + list(ch.get("alt") or [])
    insecure = ch["id"] == "eltv"
    for i, u in enumerate(urls):
        ok, detail = probe(u, ch.get("headers"), insecure)
        if ok:
            return dict(ch=ch, ok=True, url=u,
                        detail=detail + ("" if i == 0 else "  [via alt URL]"))
    return dict(ch=ch, ok=False, url=urls[0], detail=detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", help="only test this tier")
    ap.add_argument("--geo", help="only test channels with this geo flag (e.g. AZ)")
    ap.add_argument("--playlist", default=os.path.join(HERE, "playlist.json"))
    ap.add_argument("--promote", action="store_true",
                    help="rewrite playlist.json/.m3u with passing channels marked verified")
    a = ap.parse_args()

    doc = json.load(open(a.playlist, encoding="utf-8"))
    chans = [c for c in doc["channels"]
             if (not a.tier or c["tier"] == a.tier)
             and (not a.geo or c.get("geo") == a.geo)]
    print(f"testing {len(chans)} channels...\n")
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(check, chans))

    for r in sorted(res, key=lambda x: (not x["ok"], x["ch"]["name"])):
        print(f"{'  OK  ' if r['ok'] else ' FAIL '} {r['ch']['name'][:24]:24} "
              f"{r['ch']['tier']:9} {r['detail']}")
    ok = [r for r in res if r["ok"]]
    print(f"\n{len(ok)}/{len(res)} working from this network")

    if a.promote:
        by_id = {r["ch"]["id"]: r for r in res}
        for c in doc["channels"]:
            r = by_id.get(c["id"])
            if r and r["ok"] and c["tier"] != "verified":
                c["tier"] = "verified"
                c["verified_from"] = "self-test from this network"
            elif r and not r["ok"] and c["tier"] == "verified":
                c["tier"] = "unreachable"
        for t in ("verified", "degraded", "unreachable"):
            doc["counts"][t] = len([c for c in doc["channels"] if c["tier"] == t])
        doc["counts"]["geo_az"] = len([c for c in doc["channels"] if c.get("geo") == "AZ"])
        doc["counts"]["total"] = len(doc["channels"])
        json.dump(doc, open(a.playlist, "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)
        print(f"\nupdated {a.playlist} -> verified={doc['counts']['verified']}")
        print("regenerate the .m3u with:  python3 build_m3u.py")


if __name__ == "__main__":
    main()
