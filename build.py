#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py — 配布用の単一実行ファイルを作る。

    python build.py            # vendor 取得 → PyInstaller
    python build.py --fetch    # vendor の取得のみ
    python build.py --clean    # 中間生成物と vendor を削除

開発者がこれを一度実行すれば、利用者はできあがった実行ファイルを
1 回ダウンロードするだけで済む。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime as rt  # noqa: E402

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"
APP_NAME = "YTAudioDL"
SEP = ";" if rt.IS_WIN else ":"


def _bar(label: str, pct: float) -> None:
    if pct < 0:
        print(f"\r  {label} …", end="", flush=True)
    else:
        filled = int(pct / 4)
        print(f"\r  {label} [{'#' * filled:<25}] {pct:5.1f}%", end="", flush=True)


def fetch() -> None:
    """vendor/ に yt-dlp と ffmpeg を揃える。"""
    VENDOR.mkdir(exist_ok=True)

    # runtime の取得処理を一時的に vendor へ向ける
    orig_bin = rt.BIN_DIR
    rt.BIN_DIR = VENDOR
    rt.YTDLP = VENDOR / ("yt-dlp.exe" if rt.IS_WIN else "yt-dlp")
    rt.FFMPEG = VENDOR / ("ffmpeg.exe" if rt.IS_WIN else "ffmpeg")
    rt.FFPROBE = VENDOR / ("ffprobe.exe" if rt.IS_WIN else "ffprobe")
    try:
        print("[1/2] yt-dlp")
        if rt.YTDLP.exists():
            print("  既に取得済み")
        else:
            rt.ensure_ytdlp(_bar)
            print()

        print("[2/2] ffmpeg / ffprobe")
        if rt.FFMPEG.exists() and rt.FFPROBE.exists():
            print("  既に取得済み")
        else:
            rt.ensure_ffmpeg(_bar)
            print()
    finally:
        rt.BIN_DIR = orig_bin

    total = sum(p.stat().st_size for p in VENDOR.iterdir() if p.is_file())
    print(f"\nvendor/ 合計 {total / 1024 / 1024:.1f} MB")
    for p in sorted(VENDOR.iterdir()):
        if p.is_file():
            print(f"  {p.name:<16} {p.stat().st_size / 1024 / 1024:7.1f} MB")


def build(onefile: bool = True) -> None:
    if not VENDOR.is_dir() or not any(VENDOR.iterdir()):
        raise SystemExit("vendor/ が空です。先に python build.py --fetch を実行してください。")

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit("PyInstaller がありません: pip install pyinstaller")

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile" if onefile else "--onedir",
        "--windowed",
        "--name", APP_NAME,
        "--add-data", f"{VENDOR}{SEP}vendor",
        "--hidden-import", "runtime",
    ]
    icon = ROOT / ("icon.ico" if rt.IS_WIN else "icon.icns")
    if icon.exists():
        args += ["--icon", str(icon)]
    args.append(str(ROOT / "app.py"))

    print("\n$ " + " ".join(args) + "\n")
    subprocess.run(args, check=True)

    out = ROOT / "dist"
    print(f"\n完成: {out}")
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.stat().st_size > 1024 * 1024:
            print(f"  {p.relative_to(out)}  {p.stat().st_size / 1024 / 1024:.1f} MB")


def clean() -> None:
    for d in ("build", "dist", "vendor", "__pycache__"):
        shutil.rmtree(ROOT / d, ignore_errors=True)
    for f in ROOT.glob("*.spec"):
        f.unlink()
    print("削除しました")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="vendor の取得のみ")
    ap.add_argument("--clean", action="store_true", help="生成物を削除")
    ap.add_argument("--onedir", action="store_true", help="onefile ではなく onedir で作る")
    a = ap.parse_args()

    if a.clean:
        clean()
    elif a.fetch:
        fetch()
    else:
        fetch()
        build(onefile=not a.onedir)
