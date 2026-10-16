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
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

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


def _parse_time(text: str) -> float | None:
    """'90' / '1:30' / '01:02:03' / '1:30.5' を秒数に変換する。空欄は None。

    形式が不正な場合は ValueError を送出する。
    """
    t = text.strip().replace("：", ":")  # 全角コロンも受け付ける
    if not t:
        return None
    parts = t.split(":")
    if len(parts) > 3:
        raise ValueError(text)
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(text)
    if any(n < 0 for n in nums):
        raise ValueError(text)
    sec = 0.0
    for n in nums:
        sec = sec * 60 + n
    return sec


def _fmt_seconds(sec: float) -> str:
    """yt-dlp に渡す秒数表記(指数表記を避ける)。"""
    return format(sec, "f").rstrip("0").rstrip(".") or "0"


def _time_label(sec: float) -> str:
    """ファイル名に付ける時間ラベル(例: 1m30s / 1h02m03s)。コロンは使えないため。"""
    total = int(sec)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def _tk_safe(text: str) -> str:
    """Tk が表示できない BMP 外の文字(一部の絵文字など)を '?' に置き換える。

    環境によっては BMP 外の文字を Label に渡すと例外や文字化けの原因になる。
    """
    return "".join(ch if ord(ch) <= 0xFFFF else "?" for ch in text)


def _has_explicit_video_id(url: str) -> bool:
    """URLが特定の1本の動画を指しているか(=動画IDを含むか)を判定する。

    True の場合は単体動画として list/index を取り除ける。
    False の場合(例: youtube.com/playlist?list=...)は動画IDが無いため、
    単体動画に変換しようがなく、再生リストとして扱うしかない。
    """
    try:
        p = urlparse(url)
    except Exception:
        return True  # 解析できない場合は安全側(単体動画扱い)にしておく
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path

    if host == "youtu.be":
        return bool(path.strip("/"))
    if "youtube.com" in host:
        if path.startswith(("/shorts/", "/embed/", "/live/", "/v/")):
            return True
        if path == "/watch":
            qs = parse_qs(p.query)
            return bool(qs.get("v", [""])[0])
        return False  # /playlist など動画IDを含まないパス
    return True  # YouTube以外のURLはそのまま単体として扱う


def _strip_playlist_params(url: str) -> str:
    """list= / index= / start_radio= を取り除き、動画1本だけを指すURLにする。

    これにより、再生リストの途中で開いた動画や「ミックスリスト」内の動画でも、
    yt-dlp が再生リスト/ミックスの展開処理に入らず常に単体動画として扱える。
    """
    try:
        p = urlparse(url)
        qs = parse_qs(p.query)
        for key in ("list", "index", "start_radio", "pp"):
            qs.pop(key, None)
        new_query = urlencode({k: v[0] for k, v in qs.items()})
        return urlunparse(p._replace(query=new_query))
    except Exception:
        return url


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
        self.geometry("660x600")
        self.resizable(False, False)

        self.q: queue.Queue = queue.Queue()
        self.batch_queue: queue.Queue = queue.Queue()
        self.worker_running = False
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.cfg = runtime.load_config()
        self._run_token = 0

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
        ).grid(row=4, column=2, columnspan=1, sticky="e", pady=(10, 0))

        # --- 再生リストの範囲指定 (n曲目 ～ m曲目) ---
        range_frm = ttk.Frame(frm)
        range_frm.grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(range_frm, text="再生リストの範囲:").pack(side="left")
        self.pl_start = tk.StringVar(value="")
        self.pl_end = tk.StringVar(value="")
        vcmd = (self.register(lambda s: s == "" or (s.isascii() and s.isdigit())), "%P")
        ttk.Entry(range_frm, textvariable=self.pl_start, width=5,
                  validate="key", validatecommand=vcmd).pack(side="left", padx=(6, 2))
        ttk.Label(range_frm, text="曲目 ～").pack(side="left")
        ttk.Entry(range_frm, textvariable=self.pl_end, width=5,
                  validate="key", validatecommand=vcmd).pack(side="left", padx=(6, 2))
        ttk.Label(range_frm, text="曲目").pack(side="left")
        ttk.Label(range_frm, text="（空欄＝最初／最後まで）",
                  foreground="#777", font=("", 8)).pack(side="left", padx=(8, 0))

        # --- 時間の切り出し (開始 ～ 終了) ---
        time_frm = ttk.Frame(frm)
        time_frm.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(time_frm, text="切り出し(任意):").pack(side="left")
        self.sec_start = tk.StringVar(value="")
        self.sec_end = tk.StringVar(value="")
        ttk.Entry(time_frm, textvariable=self.sec_start, width=9).pack(side="left", padx=(6, 2))
        ttk.Label(time_frm, text="～").pack(side="left")
        ttk.Entry(time_frm, textvariable=self.sec_end, width=9).pack(side="left", padx=(6, 2))
        ttk.Label(time_frm, text="例: 1:30 / 0:02:15 / 90（秒）　空欄＝最初／最後まで",
                  foreground="#777", font=("", 8)).pack(side="left", padx=(8, 0))

        ttk.Label(
            frm,
            text=(
                "※オフでも、動画IDを含まない再生リストURL（例: .../playlist?list=...）は自動的に全曲取得します。\n"
                "動画1本のURLに &list=... が付いている場合は、その1本だけを取得します。\n"
                "※範囲指定は再生リスト取得時のみ有効です。ミックスリストは内容が変わることがあるため範囲で絞れます。\n"
                "※切り出しは今回追加するすべてのURL（再生リストなら各曲）に同じ時間を適用し、\n"
                "　ファイル名に [開始-終了] が付きます。範囲・切り出し欄は追加後に自動で空になります。"
            ),
            foreground="#777", font=("", 8), wraplength=600, justify="left",
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self.bar = ttk.Progressbar(frm, maximum=100)
        self.bar.grid(row=8, column=0, columnspan=4, sticky="we", pady=(14, 4))

        self.status = ttk.Label(frm, text="起動中…", anchor="w", foreground="#444")
        self.status.grid(row=9, column=0, columnspan=4, sticky="we")

        bar = ttk.Frame(frm)
        bar.grid(row=10, column=0, columnspan=4, sticky="we", pady=(12, 0))
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

    # ---------- URL の前処理 ----------
    def _prepare_urls(self, raw_urls: list[str]) -> tuple[list[str], bool]:
        """チェックボックスの状態とURLの種類に応じて、実際に渡すURLと
        再生リストとして扱うかどうかを決める。

        - チェックがオンなら全URLをそのまま渡し、再生リスト展開を有効にする。
        - チェックがオフでも、動画IDを含まない純粋な再生リストURLは
          単体動画に変換しようがないため、そのURLに限り再生リストとして扱う。
        - チェックがオフで動画IDがあるURL(ミックスリスト内の動画を含む)は、
          list/index パラメータを取り除いて確実に単体動画として扱う。
        """
        if self.playlist_mode.get():
            return raw_urls, True

        prepared = []
        force_playlist = False
        for u in raw_urls:
            if _has_explicit_video_id(u):
                prepared.append(_strip_playlist_params(u))
            else:
                prepared.append(u)
                force_playlist = True
        return prepared, force_playlist

    # ---------- 実行 ----------
    def _args(self, batch: dict) -> list[str]:
        out = Path(self.out_var.get())
        as_playlist = batch["as_playlist"]
        playlist_flag = "--yes-playlist" if as_playlist else "--no-playlist"
        # 再生リスト取得時のみ連番を付け、単体動画取得時はタイトルのみにする。
        # (playlist_index|)s: 存在すれば実際の番号、無ければ空文字
        # (playlist_index&. |)s: 存在すれば区切りの ". " のみ、無ければ空文字
        # 時間を切り出す場合は、全編を保存したファイルと名前が衝突して
        # 「ダウンロード済み」としてスキップされないよう、範囲をファイル名に付ける。
        outtmpl = (
            "%(playlist_index|)s%(playlist_index&. |)s%(title)s"
            + batch["suffix"] + ".%(ext)s"
        )
        args = [
            str(runtime.YTDLP),
            # 標準出力の文字コードをUTF-8に固定する(日本語タイトルの文字化け対策)。
            "--encoding", "utf-8",
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
        # 再生リストの範囲指定 (例: 5曲目～10曲目 → "5:10"、5曲目以降 → "5:"、先頭～10曲目 → "1:10")
        if as_playlist and (batch["pl_start"] or batch["pl_end"]):
            start = batch["pl_start"] or "1"
            end = batch["pl_end"] or ""
            args += ["--playlist-items", f"{start}:{end}"]
        # 時間の切り出し (例: "*90-165"、終了を省略すると "*90-inf")
        if batch["sec_start"] is not None or batch["sec_end"] is not None:
            s = _fmt_seconds(batch["sec_start"] or 0.0)
            e = "inf" if batch["sec_end"] is None else _fmt_seconds(batch["sec_end"])
            args += ["--download-sections", f"*{s}-{e}"]
        if self.meta.get():
            args += ["--embed-thumbnail", "--embed-metadata"]
        return args + batch["urls"]

    def _start(self):
        if not runtime.is_ready():
            messagebox.showwarning("未準備", "コンポーネントの取得が完了していません。")
            return
        raw_urls = [u.strip() for u in self.urls.get("1.0", "end").splitlines() if u.strip()]
        if not raw_urls:
            messagebox.showinfo("URLなし", "URLを入力してください。")
            return
        # --- 再生リストの範囲の検証 ---
        pl_start = self.pl_start.get().strip()
        pl_end = self.pl_end.get().strip()
        if pl_start == "0" or pl_end == "0":
            messagebox.showerror("範囲の指定", "曲の番号は 1 以上で指定してください。")
            return
        if pl_start and pl_end and int(pl_start) > int(pl_end):
            messagebox.showerror(
                "範囲の指定", "開始の曲番号が終了より大きくなっています。"
            )
            return

        # --- 時間の切り出しの検証 ---
        try:
            sec_start = _parse_time(self.sec_start.get())
            sec_end = _parse_time(self.sec_end.get())
        except ValueError:
            messagebox.showerror(
                "時間の形式",
                "時間は「90」「1:30」「0:02:15」のような形式で入力してください。",
            )
            return
        if sec_start is not None and sec_end is not None and sec_start >= sec_end:
            messagebox.showerror("時間の指定", "開始は終了より前の時刻にしてください。")
            return

        os.makedirs(self.out_var.get(), exist_ok=True)
        self._save_settings()

        urls, as_playlist = self._prepare_urls(raw_urls)

        suffix = ""
        if sec_start is not None or sec_end is not None:
            s_label = _time_label(sec_start or 0.0)
            e_label = "end" if sec_end is None else _time_label(sec_end)
            suffix = f" [{s_label}-{e_label}]"

        batch = {
            "urls": urls,
            "as_playlist": as_playlist,
            "pl_start": pl_start,
            "pl_end": pl_end,
            "sec_start": sec_start,
            "sec_end": sec_end,
            "suffix": suffix,
        }

        # ダウンロード中でも次々に追加できるよう、キューに積むだけにする。
        # 実際の処理はワーカースレッドが順番に取り出して行う。
        self.batch_queue.put(batch)
        self.urls.delete("1.0", "end")  # 投入済みなのですぐに欄をクリアする
        # 範囲・切り出しは「今回のURLだけ」に効かせたいので、次の入力に
        # 意図せず引き継がれて全編ダウンロードのつもりが切り出しになる事故を防ぐ。
        for var in (self.pl_start, self.pl_end, self.sec_start, self.sec_end):
            var.set("")
        self.cancel_btn["state"] = "normal"

        if self.worker_running:
            self.status["text"] = f"キューに追加しました（待機中の項目あり）"
        else:
            self.worker_running = True
            self.cancelled = False
            threading.Thread(target=self._worker_loop, daemon=True).start()

    def _worker_loop(self):
        """キューにある限り、順番にダウンロードを実行し続ける。"""
        while True:
            try:
                batch = self.batch_queue.get_nowait()
            except queue.Empty:
                break
            if self.cancelled:
                # 中止後に残っていたキューは処理せず破棄する。
                continue
            self._run_token += 1
            self.q.put(("batch_start", batch["as_playlist"]))
            self._run_one(batch)
        self.worker_running = False
        self.q.put(("queue_empty", None))

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

    def _run_one(self, batch: dict):
        try:
            # 日本語Windowsでは、パイプ経由の子プロセスの標準出力が既定で
            # cp932 になり、こちらがUTF-8として読むと文字化けする。
            # 環境変数でも UTF-8 を強制する(--encoding との二重の対策)。
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            self.proc = subprocess.Popen(
                self._args(batch),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, env=env, **runtime.no_window_kwargs(),
                **runtime.process_group_kwargs(),
            )
            for line in self.proc.stdout:
                self._handle_line(ANSI_RE.sub("", line.strip()))
            code = self.proc.wait()
            if self.cancelled:
                self.q.put(("item_done", "中止しました", False))
            elif code == 0:
                self.q.put(("item_done", "完了しました", True))
            else:
                self.q.put(("item_done", f"一部または全部が失敗しました（コード {code}）", False))
        except Exception as e:
            self.q.put(("item_done", f"エラー: {e}", False))
        finally:
            self.proc = None

    def _handle_line(self, line: str):
        if PROG_RE.match(line):
            parts = line.split("|")
            if len(parts) >= 5:
                pct_s, speed, eta = parts[1], parts[2], parts[3]
                # タイトル自体に "|" が含まれていても欠けないよう、残りを結合する。
                title = _tk_safe("|".join(parts[4:]))
                try:
                    pct = float(pct_s.strip().rstrip("%"))
                except ValueError:
                    # 時間の切り出し中などは割合が取れない(N/A)ことがある。
                    self.q.put(("status", f"{title[:40]}  取得中…"))
                    return
                self.q.put(("prog", pct,
                            f"{title[:40]}  {pct:5.1f}%  {speed}  残り {eta}"))
        elif "[ExtractAudio]" in line or "Destination" in line:
            self.q.put(("status", "音声を変換しています…"))
        elif line.startswith("ERROR"):
            self.q.put(("status", _tk_safe(line[:90])))

    def _cancel(self):
        self.cancelled = True
        self.cancel_btn["state"] = "disabled"
        self.status["text"] = "中止しています…"
        # 残っているキュー項目も処理せずに捨てる。
        try:
            while True:
                self.batch_queue.get_nowait()
        except queue.Empty:
            pass
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
                elif kind == "batch_start":
                    as_playlist = rest[0]
                    self.bar["value"] = 0
                    self.status["text"] = (
                        "再生リストを取得しています…" if as_playlist else "取得しています…"
                    )
                elif kind == "item_done":
                    msg, success = rest
                    self.status["text"] = msg
                    self.bar["value"] = 0
                    if not success and not self.cancelled:
                        # 失敗しても次のキュー項目があれば続けて処理されるため、
                        # ここでは表示だけ更新し、URL欄のクリアは行わない。
                        pass
                elif kind == "queue_empty":
                    self.cancel_btn["state"] = "disabled"
                    token = self._run_token
                    self.after(3000, lambda t=token: self._reset_status_if_idle(t))
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _reset_status_if_idle(self, token: int):
        # 待っている間に次のダウンロードが始まっていたら(トークンが変わっていたら)
        # その進捗表示を上書きしないようにする。
        if token == self._run_token:
            self.status["text"] = "準備完了"


if __name__ == "__main__":
    App().mainloop()
