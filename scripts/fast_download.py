#!/usr/bin/env python3
"""Multi-connection download via aria2c (generic files: images, audio, archives, models...).

Examples:
  fast_download.py https://example.com/a.mp4 -o downloads/a.mp4
  fast_download.py URL1 URL2 URL3 -d downloads/ -j 4
  fast_download.py -i urls.txt -d downloads/ --header "Referer: https://site/"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mmcommon import aria2_args, aria2_env_args, main_guard, require_bin, run, MMError  # noqa: E402


@main_guard
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*", help="URL(s) to download")
    ap.add_argument("-i", "--input-file", help="aria2 input file / plain list of URLs (one per line)")
    ap.add_argument("-o", "--output", help="output file path (only with a single URL)")
    ap.add_argument("-d", "--dir", default=".", help="output directory (default: .)")
    ap.add_argument("-x", "--connections", type=int, default=None, help="connections per file (1-16, default ARIA2_CONNECTIONS or 16)")
    ap.add_argument("-j", "--jobs", type=int, default=4, help="parallel files (default 4)")
    ap.add_argument("--header", action="append", default=[], help="extra HTTP header, repeatable")
    ap.add_argument("--cookies", help="Netscape cookies.txt for authenticated downloads")
    a = ap.parse_args()

    if not a.urls and not a.input_file:
        ap.error("need at least one URL or --input-file")
    if a.output and len(a.urls) != 1:
        ap.error("--output only works with exactly one URL")

    aria2c = require_bin("aria2c", "多线程下载需要 aria2：apt install aria2 / brew install aria2 / choco install aria2")
    out_dir = Path(a.output).expanduser().resolve().parent if a.output else Path(a.dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [aria2c, *aria2_args(a.connections), f"--max-concurrent-downloads={max(1, a.jobs)}", "-d", str(out_dir)]
    for h in a.header:
        cmd += ["--header", h]
    if a.cookies:
        cmd += [f"--load-cookies={Path(a.cookies).expanduser()}"]
    cmd += aria2_env_args()
    if a.output:
        cmd += ["-o", Path(a.output).name]
    if a.input_file:
        cmd += ["-i", a.input_file]
    cmd += a.urls

    proc = run(cmd, check=False)
    if proc.returncode != 0:
        raise MMError(f"aria2c 退出码 {proc.returncode}（见上方日志）。若是 403/鉴权问题，请提供 --cookies 或 --header。")
    print(f"[OK] saved to {out_dir}")


if __name__ == "__main__":
    main()
