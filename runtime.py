#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
runtime.py — 依存バイナリ（yt-dlp / ffmpeg）の自動取得・更新。

アプリ初回起動時にユーザー領域へダウンロードするため、
利用者は本体を落として実行するだけで済む。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "YTAudioDL"
UA = {"User-Agent": f"{APP_NAME}/1.0"}

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
ARM = platform.machine().lower() in ("arm64", "aarch64")

# --- 取得元 ---------------------------------------------------------------
# GitHub の /releases/latest/download/ は常に最新版へ解決される。
YTDLP_URL = {
    "win32": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    "darwin": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos",
    "linux": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux",
}
FFMPEG_URL = {
    # BtbN の /releases/latest/download/ は「一番新しく公開されたリリース」を指すため、
    # 日付単位のビルドが公開されるとファイル名が変わり404になる。
    # 固定タグ "latest" のリリースを直接指すことでこれを回避する。
    "win32": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
             "ffmpeg-master-latest-win64-gpl.zip",
    "linux": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
             "ffmpeg-master-latest-linux64-gpl.tar.xz",
    "darwin": "https://evermeet.cx/ffmpeg/getrelease/zip",
}
FFPROBE_MAC = "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip"

UPDATE_INTERVAL = 60 * 60 * 24 * 3  # yt-dlp の更新確認間隔（3日）


# --- 配置先 ---------------------------------------------------------------
def app_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif IS_MAC:
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    d = base / APP_NAME
    (d / "bin").mkdir(parents=True, exist_ok=True)
    return d


BIN_DIR = app_dir() / "bin"
STATE_FILE = app_dir() / "state.json"


def _exe(name: str) -> Path:
    return BIN_DIR / (name + (".exe" if IS_WIN else ""))


YTDLP = _exe("yt-dlp")
FFMPEG = _exe("ffmpeg")
FFPROBE = _exe("ffprobe")

CONFIG_FILE = app_dir() / "config.json"

DEFAULT_CONFIG = {
    "output_dir": str(Path.home() / "Downloads"),
    "bitrate": "192",
    "embed_metadata": True,
}


def load_config() -> dict:
    """前回終了時の設定を読み込む。無ければ既定値を返す。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def no_window_kwargs() -> dict:
    """Windows でコンソールを一瞬表示させないための起動オプション。"""
    if IS_WIN:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def process_group_kwargs() -> dict:
    """子プロセスをまとめて停止できるよう、専用のプロセスグループで起動する。"""
    if IS_WIN:
        # CREATE_NO_WINDOW と両立させるため no_window_kwargs 側でまとめて指定する。
        return {}
    return {"start_new_session": True}


def kill_process_tree(proc: subprocess.Popen) -> None:
    """子プロセス(ffmpeg等)ごと確実に停止する。

    yt-dlp はダウンロード後に ffmpeg を子プロセスとして起動するため、
    proc.terminate() だけでは ffmpeg が生き残り、標準出力パイプが
    閉じずに読み取りループが固まることがある。プロセスツリーごと落とす。
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10, **no_window_kwargs(),
            )
        else:
            import signal
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# --- 同梱バイナリ ---------------------------------------------------------
def bundle_dir() -> Path | None:
    """実行ファイルに同梱された vendor/ を返す。無ければ None。"""
    meipass = getattr(sys, "_MEIPASS", None)          # PyInstaller 実行時
    candidates = [
        Path(meipass) / "vendor" if meipass else None,
        Path(__file__).resolve().parent / "vendor",    # 開発時
        Path(sys.executable).resolve().parent / "vendor",  # onedir 配布時
    ]
    for c in candidates:
        if c and c.is_dir():
            return c
    return None


def _install_from_bundle(names: set[str]) -> bool:
    """同梱物を BIN_DIR へコピーする。全て揃えられたら True。"""
    src = bundle_dir()
    if src is None:
        return False
    ok = True
    for n in names:
        fname = n + (".exe" if IS_WIN else "")
        s = src / fname
        if not s.is_file():
            ok = False
            continue
        d = BIN_DIR / fname
        if not d.exists():
            shutil.copy2(s, d)
            _make_executable(d)
    return ok


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(d: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


# --- ダウンロード ---------------------------------------------------------
def _fetch(url: str, dest: Path, on_progress=None, label: str = "") -> None:
    req = urllib.request.Request(url, headers=UA)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=60) as res, open(tmp, "wb") as f:
        total = int(res.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = res.read(262144)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if on_progress:
                pct = got / total * 100 if total else -1
                on_progress(label, pct)
    tmp.replace(dest)


def _make_executable(p: Path) -> None:
    if not IS_WIN:
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _extract_binaries(archive: Path, names: set[str]) -> None:
    """書庫の中から目的の実行ファイルだけを BIN_DIR 直下へ取り出す。"""
    wanted = {n + (".exe" if IS_WIN else "") for n in names}

    def emit(name: str, reader) -> None:
        base = Path(name).name
        if base in wanted:
            out = BIN_DIR / base
            with open(out, "wb") as f:
                shutil.copyfileobj(reader, f)
            _make_executable(out)

    if archive.suffix == ".zip" or zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if not info.is_dir():
                    with z.open(info) as r:
                        emit(info.filename, r)
    else:
        with tarfile.open(archive, "r:*") as t:
            for m in t.getmembers():
                if m.isfile():
                    r = t.extractfile(m)
                    if r:
                        emit(m.name, r)


# --- 各コンポーネント -----------------------------------------------------
def ensure_ytdlp(on_progress=None) -> Path:
    if YTDLP.exists():
        return YTDLP
    if _install_from_bundle({"yt-dlp"}) and YTDLP.exists():
        return YTDLP
    # 同梱されていない場合のみネットワークから取得（開発時・復旧時）
    url = YTDLP_URL["win32" if IS_WIN else "darwin" if IS_MAC else "linux"]
    _fetch(url, YTDLP, on_progress, "yt-dlp を取得しています")
    _make_executable(YTDLP)
    st = _state()
    st["ytdlp_checked"] = time.time()
    _save_state(st)
    return YTDLP


def ensure_ffmpeg(on_progress=None) -> Path:
    if FFMPEG.exists() and FFPROBE.exists():
        return FFMPEG
    if _install_from_bundle({"ffmpeg", "ffprobe"}) and FFMPEG.exists() and FFPROBE.exists():
        return FFMPEG
    # 同梱されていない場合のみネットワークから取得（開発時・復旧時）
    key = "win32" if IS_WIN else "darwin" if IS_MAC else "linux"
    with tempfile.TemporaryDirectory() as td:
        arc = Path(td) / "ffmpeg.archive"
        _fetch(FFMPEG_URL[key], arc, on_progress, "ffmpeg を取得しています")
        _extract_binaries(arc, {"ffmpeg", "ffprobe"})
        if IS_MAC and not FFPROBE.exists():
            arc2 = Path(td) / "ffprobe.zip"
            _fetch(FFPROBE_MAC, arc2, on_progress, "ffprobe を取得しています")
            _extract_binaries(arc2, {"ffprobe"})
    if not FFMPEG.exists():
        raise RuntimeError("ffmpeg の展開に失敗しました")
    return FFMPEG


def update_ytdlp(force: bool = False) -> str:
    """yt-dlp の自己更新。YouTube 側の仕様変更に追従するため定期的に実行する。"""
    if not YTDLP.exists():
        return "未インストール"
    st = _state()
    last = st.get("ytdlp_checked", 0)
    if not force and time.time() - last < UPDATE_INTERVAL:
        return "更新確認はスキップ"
    try:
        r = subprocess.run(
            [str(YTDLP), "-U"], capture_output=True, text=True,
            timeout=120, **no_window_kwargs()
        )
        st["ytdlp_checked"] = time.time()
        _save_state(st)
        tail = (r.stdout or "").strip().splitlines()
        return tail[-1] if tail else "更新確認完了"
    except Exception as e:
        return f"更新確認に失敗: {e}"


def ensure_all(on_status=None, on_progress=None) -> None:
    """初回起動時のセットアップ一式。

    同梱されている場合はコピーのみで完結し、通信は発生しない。
    """
    def status(msg):
        if on_status:
            on_status(msg)

    bundled = bundle_dir() is not None

    if not YTDLP.exists():
        status("コンポーネントを展開しています…" if bundled
               else "yt-dlp をダウンロードしています…")
    ensure_ytdlp(on_progress)

    if not (FFMPEG.exists() and FFPROBE.exists()):
        status("音声変換エンジンを展開しています…" if bundled
               else "ffmpeg をダウンロードしています…")
    ensure_ffmpeg(on_progress)

    # 更新確認は任意。オフラインでも失敗させない。
    status("準備しています…")
    try:
        update_ytdlp()
    except Exception:
        pass
    status("準備完了")


def is_ready() -> bool:
    return YTDLP.exists() and FFMPEG.exists() and FFPROBE.exists()


def reset() -> None:
    """バイナリを削除して再取得させる（トラブル時の復旧用）。"""
    shutil.rmtree(BIN_DIR, ignore_errors=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.unlink(missing_ok=True)
