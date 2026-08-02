#!/usr/bin/env python3
"""Regenerate playlist.m3u from playlist.json.

Run this after `selftest.py --promote` so the .m3u picks up any geo-locked
channels that turned out to work on your network.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
doc = json.load(open(os.path.join(HERE, "playlist.json"), encoding="utf-8"))

lines = ['#EXTM3U x-tvg-url="https://azepg.ddns.net/aztv/"']
for c in doc["channels"]:
    if c["tier"] != "verified":
        continue
    attrs = f'tvg-id="{c["id"]}" tvg-name="{c["name"]}"'
    if c.get("logo"):
        attrs += f' tvg-logo="{c["logo"]}"'
    attrs += f' group-title="{c["group"]}"'
    lines.append(f'#EXTINF:-1 {attrs},{c["name"]}')
    for k, v in (c.get("headers") or {}).items():
        lines.append(f"#EXTVLCOPT:http-{k.lower()}={v}")
    lines.append(c["url"])

open(os.path.join(HERE, "playlist.m3u"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
print(f"playlist.m3u: {len([c for c in doc['channels'] if c['tier']=='verified'])} channels")

# playlist-all.m3u -- verified channels PLUS the AZ-geo-locked ones, for
# testing from an Azerbaijani connection where the TV itself is the check.
# The untested ones are grouped separately so it's obvious which is which.
allc = []
def with_alts(c, group):
    out = [(c, group, c["url"], c["name"])]
    for i, u in enumerate(c.get("alt") or []):
        label = f"{c['name']} (alt)" if len(c.get("alt") or []) == 1 else f"{c['name']} (alt {i+1})"
        out.append((c, group, u, label))
    return out

for c in doc["channels"]:
    if c["tier"] == "verified":
        allc += with_alts(c, c["group"])
for c in doc["channels"]:
    if c["tier"] == "az_only":
        allc += with_alts(c, "AZ-only (untested)")

lines = ['#EXTM3U x-tvg-url="https://azepg.ddns.net/aztv/"']
for c, grp, url, label in allc:
    attrs = f'tvg-id="{c["id"]}" tvg-name="{label}"'
    if c.get("logo"):
        attrs += f' tvg-logo="{c["logo"]}"'
    attrs += f' group-title="{grp}"'
    lines.append(f'#EXTINF:-1 {attrs},{label}')
    # Only the primary castr edge needs the Referer; the alts do not.
    if url == c["url"]:
        for k, v in (c.get("headers") or {}).items():
            lines.append(f"#EXTVLCOPT:http-{k.lower()}={v}")
    lines.append(url)

open(os.path.join(HERE, "playlist-all.m3u"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
print(f"playlist-all.m3u: {len(allc)} channels")
