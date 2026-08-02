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
