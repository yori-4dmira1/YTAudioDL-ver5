#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — YouTube Audio Downloader

yt-dlp を子プロセスとして呼び出し、進捗を標準出力から読み取る。
依存バイナリは runtime.py が自動で用意するため、利用者の準備は不要。

権利上ダウンロードが許されている動画にのみ使用してください。
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import runtime

BITRATES = ["128", "192", "256", "320"]
AUDIO_FORMAT = "m4a"  # 固定。Windows標準環境で追加コーデック無しに再生・サムネ表示できる形式。
# YouTube 側が既に m4a/aac で配信している場合はそれを直接使うことで
# 音声の再エンコードを避け、変換にかかる時間を大幅に短縮する。
FORMAT_SELECTOR = "bestaudio[ext=m4a]/bestaudio/best"
CONCURRENT_FRAGMENTS = "4"

# yt-dlp に機械可読な進捗を吐かせるためのテンプレート
PROGRESS_TEMPLATE = (
    "PROG|%(progress._percent_str)s|%(progress._speed_str)s|"
    "%(progress._eta_str)s|%(info.title)s"
)
PROG_RE = re.compile(r"^PROG\|")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ==========================================================================
# 初回セットアップ画面
# ==========================================================================
class SetupWindow(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("初期設定")
        self.geometry("420x150")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self.ok = False
        self.q: queue.Queue = queue.Queue()

        ttk.Label(self, text="必要なコンポーネントを準備しています").pack(pady=(20, 4))
        self.msg = ttk.Label(self, text="接続しています…", foreground="#555")
        self.msg.pack()
        self.bar = ttk.Progressbar(self, mode="indeterminate", length=340)
        self.bar.pack(pady=16)
        self.bar.start(12)

        self.transient(master)
        self.grab_set()
        threading.Thread(target=self._work, daemon=True).start()
        self.after(100, self._poll)

    def _work(self):
        try:
            runtime.ensure_all(
                on_status=lambda m: self.q.put(("status", m)),
                on_progress=lambda label, pct: self.q.put(("prog", label, pct)),
            )
            self.q.put(("done", None))
        except Exception as e:
            self.q.put(("fail", e))

    def _poll(self):
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "status":
                    self.msg["text"] = rest[0]
                elif kind == "prog":
                    label, pct = rest
                    if pct >= 0:
                        if self.bar["mode"] != "determinate":
                            self.bar.stop()
                            self.bar.configure(mode="determinate", maximum=100)
                        self.bar["value"] = pct
                        self.msg["text"] = f"{label}  {pct:.0f}%"
                elif kind == "done":
                    self.ok = True
                    self.destroy()
                    return
                elif kind == "fail":
                    self.bar.stop()
                    messagebox.showerror(
                        "セットアップ失敗",
                        "必要なファイルを取得できませんでした。\n"
                        "ネットワーク接続やセキュリティソフトの設定をご確認ください。\n\n"
                        f"詳細: {rest[0]}",
                        parent=self,
                    )
                    self.destroy()
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll)


# ==========================================================================
# メインウィンドウ
# ==========================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Audio Downloader")
        self.geometry("640x470")
        self.resizable(False, False)

        self.q: queue.Queue = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.cfg = runtime.load_config()

        self._build_ui()
        self.after(100, self._poll)
        self.after(200, self._setup)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- セットアップ ----------
    def _setup(self):
        if runtime.is_ready():
            threading.Thread(target=self._bg_update, daemon=True).start()
            return
        self.withdraw()
        win = SetupWindow(self)
        self.wait_window(win)
        if not win.ok:
            self.destroy()
            return
        self.deiconify()

    def _bg_update(self):
        msg = runtime.update_ytdlp()
        self.q.put(("status", f"準備完了（{msg}）"))

    # ---------- UI ----------
    def _build_ui(self):
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=14, pady=12)

        ttk.Label(frm, text="URL（動画 / 再生リスト、1行に1つ）").grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        self.urls = tk.Text(frm, height=6, width=72)
        self.urls.grid(row=1, column=0, columnspan=4, sticky="we", pady=(2, 10))

        ttk.Label(frm, text="保存先").grid(row=2, column=0, sticky="w")
        self.out_var = tk.StringVar(value=self.cfg["output_dir"])
        ttk.Entry(frm, textvariable=self.out_var).grid(
            row=2, column=1, columnspan=2, sticky="we", padx=4
        )
        ttk.Button(frm, text="参照…", width=8, command=self._choose_dir).grid(
            row=2, column=3, sticky="e"
        )

        ttk.Label(frm, text="音質(kbps)").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.br = tk.StringVar(value=self.cfg["bitrate"])
        self.br_box = ttk.Combobox(frm, textvariable=self.br, values=BITRATES,
                                   state="readonly", width=8)
        self.br_box.grid(row=3, column=1, sticky="w", pady=(10, 0))
        ttk.Label(frm, text="出力形式: m4a 固定", foreground="#666").grid(
            row=3, column=2, columnspan=2, sticky="e", pady=(10, 0)
        )

        self.meta = tk.BooleanVar(value=self.cfg["embed_metadata"])
        ttk.Checkbutton(
            frm, text="サムネイル画像・曲情報をファイルに埋め込む", variable=self.meta
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.playlist_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frm,
            text="再生リストのURLとして扱う（全曲まとめてダウンロード）",
            variable=self.playlist_mode,
        ).grid(row=4, column=2, columnspan=2, sticky="e", pady=(10, 0))

        ttk.Label(
            frm,
            text="※オフの場合、URLに &list=... が含まれていてもその動画1本だけを取得します。",
            foreground="#777", font=("", 8), wraplength=580, justify="left",
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(2, 0))

        self.bar = ttk.Progressbar(frm, maximum=100)
        self.bar.grid(row=6, column=0, columnspan=4, sticky="we", pady=(14, 4))

        self.status = ttk.Label(frm, text="起動中…", anchor="w", foreground="#444")
        self.status.grid(row=7, column=0, columnspan=4, sticky="we")

        bar = ttk.Frame(frm)
        bar.grid(row=8, column=0, columnspan=4, sticky="we", pady=(12, 0))
        ttk.Button(bar, text="コンポーネント再取得", command=self._repair).pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="中止", command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="right", padx=(6, 0))
        self.dl_btn = ttk.Button(bar, text="ダウンロード", command=self._start)
        self.dl_btn.pack(side="right")

        frm.columnconfigure(1, weight=1)

    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get())
        if d:
            self.out_var.set(d)

    def _repair(self):
        if not messagebox.askyesno(
            "再取得", "yt-dlp と ffmpeg を削除して取得し直します。よろしいですか？"
        ):
            return
        runtime.reset()
        win = SetupWindow(self)
        self.wait_window(win)

    # ---------- 実行 ----------
    def _args(self, urls: list[str]) -> list[str]:
        out = Path(self.out_var.get())
        # 再生リストのURLでも、通常は末尾の &list=... を無視してその動画1本だけを
        # 取得する(--no-playlist)。「再生リストとして扱う」がオンの時だけ全曲取得する。
        playlist_flag = "--yes-playlist" if self.playlist_mode.get() else "--no-playlist"
        # 再生リスト取得時は動画ごとに連番を付け、単体動画取得時はタイトルのみにする。
        outtmpl = "%(playlist_index&{}. |)s%(title)s.%(ext)s"
        args = [
            str(runtime.YTDLP),
            "--ffmpeg-location", str(runtime.BIN_DIR),
            "--paths", str(out),
            "-o", outtmpl,
            "-f", FORMAT_SELECTOR,
            "-x", "--audio-format", AUDIO_FORMAT,
            "--audio-quality", f"{self.br.get()}K",
            "--concurrent-fragments", CONCURRENT_FRAGMENTS,
            playlist_flag,
            "--newline", "--no-colors", "--no-warnings",
            "--progress-template", PROGRESS_TEMPLATE,
            "--ignore-errors",
        ]
        if self.meta.get():
            args += ["--embed-thumbnail", "--embed-metadata"]
        return args + urls

    def _start(self):
        if not runtime.is_ready():
            messagebox.showwarning("未準備", "コンポーネントの取得が完了していません。")
            return
        urls = [u.strip() for u in self.urls.get("1.0", "end").splitlines() if u.strip()]
        if not urls:
            messagebox.showinfo("URLなし", "URLを入力してください。")
            return
        os.makedirs(self.out_var.get(), exist_ok=True)
        self._save_settings()

        self.cancelled = False
        self.dl_btn["state"] = "disabled"
        self.cancel_btn["state"] = "normal"
        self.bar["value"] = 0
        threading.Thread(target=self._run, args=(urls,), daemon=True).start()

    def _save_settings(self):
        self.cfg.update({
            "output_dir": self.out_var.get(),
            "bitrate": self.br.get(),
            "embed_metadata": bool(self.meta.get()),
        })
        runtime.save_config(self.cfg)

    def _on_close(self):
        self._save_settings()
        if self.proc:
            runtime.kill_process_tree(self.proc)
        self.destroy()

    def _run(self, urls):
        try:
            self.proc = subprocess.Popen(
                self._args(urls),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, **runtime.no_window_kwargs(),
                **runtime.process_group_kwargs(),
            )
            for line in self.proc.stdout:
                self._handle_line(ANSI_RE.sub("", line.strip()))
            code = self.proc.wait()
            if self.cancelled:
                self.q.put(("done", "中止しました", False))
            elif code == 0:
                self.q.put(("done", "完了しました", True))
            else:
                self.q.put(("done", f"一部または全部が失敗しました（コード {code}）", False))
        except Exception as e:
            self.q.put(("done", f"エラー: {e}", False))
        finally:
            self.proc = None

    def _handle_line(self, line: str):
        if PROG_RE.match(line):
            parts = line.split("|")
            if len(parts) >= 5:
                pct_s, speed, eta, title = parts[1], parts[2], parts[3], parts[4]
                try:
                    pct = float(pct_s.strip().rstrip("%"))
                except ValueError:
                    pct = 0.0
                self.q.put(("prog", pct,
                            f"{title[:40]}  {pct:5.1f}%  {speed}  残り {eta}"))
        elif "[ExtractAudio]" in line or "Destination" in line:
            self.q.put(("status", "音声を変換しています…"))
        elif line.startswith("ERROR"):
            self.q.put(("status", line[:90]))

    def _cancel(self):
        self.cancelled = True
        self.cancel_btn["state"] = "disabled"
        self.status["text"] = "中止しています…"
        if self.proc:
            threading.Thread(
                target=runtime.kill_process_tree, args=(self.proc,), daemon=True
            ).start()

    def _poll(self):
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "prog":
                    self.bar["value"] = rest[0]
                    self.status["text"] = rest[1]
                elif kind == "status":
                    self.status["text"] = rest[0]
                elif kind == "done":
                    msg, success = rest
                    self.status["text"] = msg
                    self.bar["value"] = 0
                    self.dl_btn["state"] = "normal"
                    self.cancel_btn["state"] = "disabled"
                    if success:
                        self.urls.delete("1.0", "end")
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
