"""
╔══════════════════════════════════════════════════════════╗
║  GRAB THE INTERNET — Telegram Bot                        ║
║  YouTube · TikTok · Instagram · SoundCloud               ║
╚══════════════════════════════════════════════════════════╝

УСТАНОВКА:
    pip install python-telegram-bot yt-dlp

ffmpeg обязателен:
    macOS   → brew install ffmpeg
    Linux   → sudo apt install ffmpeg
    Windows → https://ffmpeg.org/download.html (добавить bin/ в PATH)

НАСТРОЙКА:
    Укажи токен бота в переменной BOT_TOKEN ниже (или через env).
    Получить токен: @BotFather → /newbot

ЗАПУСК:
    python tg_grab_bot.py

ЛИМИТЫ TELEGRAM:
    Стандартный Bot API — файлы до 50 МБ.
    Для тяжёлых видео рекомендуется локальный Bot API Server
    (https://github.com/tdlib/telegram-bot-api) который снимает ограничение.
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path



import yt_dlp
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)

import static_ffmpeg
static_ffmpeg.add_paths()

from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_СЮДА")

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "tg_grab"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Telegram Bot API file size limit (bytes). Default: 50 MB.
# If you run a local Bot API server, set this to 2_000_000_000.
TG_MAX_BYTES = 2_000_000_000

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("grab-bot")

# ──────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────

def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|\n\r]', "_", name)[:80]


def fmt_bytes(b: int) -> str:
    if b >= 1_000_000:
        return f"{b/1_000_000:.1f} MB"
    if b >= 1_000:
        return f"{b/1_000:.0f} KB"
    return f"{b} B"


def fmt_speed(bps: float) -> str:
    if bps <= 0:
        return "—"
    if bps >= 1_000_000:
        return f"{bps/1_000_000:.1f} MB/s"
    if bps >= 1_000:
        return f"{bps/1_000:.0f} KB/s"
    return f"{bps:.0f} B/s"


def fmt_eta(secs: float) -> str:
    if secs <= 0 or secs > 86400:
        return "—"
    secs = int(secs)
    if secs >= 3600:
        h, r = divmod(secs, 3600)
        m, s = divmod(r, 60)
        return f"{h}h {m:02d}m"
    if secs >= 60:
        m, s = divmod(secs, 60)
        return f"{m}m {s:02d}s"
    return f"{secs}s"


def progress_bar(pct: int, width: int = 14) -> str:
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


# ──────────────────────────────────────────────────────────────
# URL DETECTION
# ──────────────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    url_l = url.lower()
    if re.search(r'(tiktok\.com|vm\.tiktok\.com)', url_l):
        return "tt"
    if re.search(r'(instagram\.com|instagr\.am)', url_l):
        return "ig"
    if re.search(r'(soundcloud\.com)', url_l):
        return "sc"
    if re.search(r'(youtube\.com|youtu\.be)', url_l):
        return "yt"
    return "unknown"


# ──────────────────────────────────────────────────────────────
# KEYBOARDS
# ──────────────────────────────────────────────────────────────

def kb_youtube(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵 MP3 · 128k", callback_data=f"dl:{job_id}:mp3:128"),
            InlineKeyboardButton("🎵 MP3 · 192k", callback_data=f"dl:{job_id}:mp3:192"),
            InlineKeyboardButton("🎵 MP3 · 320k", callback_data=f"dl:{job_id}:mp3:320"),
        ],
        [
            InlineKeyboardButton("🎬 720p",        callback_data=f"dl:{job_id}:mp4:720"),
            InlineKeyboardButton("🎬 1080p",       callback_data=f"dl:{job_id}:mp4:1080"),
            InlineKeyboardButton("🎬 Max качество",callback_data=f"dl:{job_id}:mp4:best"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{job_id}")],
    ])


def kb_doom(job_id: str, platform: str) -> InlineKeyboardMarkup:
    """TikTok / Instagram keyboard."""
    icon = "📱" if platform == "tt" else "📸"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"{icon} Видео · MP4",  callback_data=f"dl:{job_id}:mp4:best"),
            InlineKeyboardButton("🎵 Аудио · MP3", callback_data=f"dl:{job_id}:mp3:192"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{job_id}")],
    ])


def kb_soundcloud(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟠 128k", callback_data=f"dl:{job_id}:sc:128"),
            InlineKeyboardButton("🟠 192k", callback_data=f"dl:{job_id}:sc:192"),
            InlineKeyboardButton("🟠 320k", callback_data=f"dl:{job_id}:sc:320"),
        ],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel:{job_id}")],
    ])


# ──────────────────────────────────────────────────────────────
# DOWNLOAD JOBS
# ──────────────────────────────────────────────────────────────

TT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.tiktok.com/",
}

IG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.instagram.com/",
}


class DownloadJob:
    """Tracks state of a single download task."""

    def __init__(self, job_id: str, url: str, platform: str, mode: str, quality: str):
        self.job_id   = job_id
        self.url      = url
        self.platform = platform
        self.mode     = mode       # "mp3" | "mp4" | "sc"
        self.quality  = quality    # "128"/"192"/"320"/"720"/"1080"/"best"

        self.status   = "starting"
        self.title    = ""
        self.progress = 0
        self.speed    = "—"
        self.eta      = "—"
        self.file: Path | None = None
        self.filename = ""
        self.mime     = "application/octet-stream"
        self.size_str = ""
        self.error: str | None = None
        self.actual_res = ""

        # SoundCloud multi-track
        self.files: list[Path] = []
        self.total_tracks = 0
        self.done_tracks  = 0
        self.sc_dir: Path | None = None

        # Internal stream tracking (same dual-stream logic as the original)
        self._streams_total = [0, 0]
        self._streams_done  = [0, 0]
        self._stream_index  = 0
        self._last_known_total = 0
        self._speed_bps    = 0.0
        self._stream_weights = [1.0]

    # ── progress hook ───────────────────────────────────────────

    def _hook(self, d: dict):
        if d["status"] == "downloading":
            self.status = "downloading"
            done  = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0

            dual = len(self._stream_weights) == 2

            if dual:
                si = self._stream_index
                if si == 0 and self._streams_done[0] > 0 and done < self._streams_done[0] * 0.1:
                    self._stream_index = 1
                    si = 1
                self._streams_done[si] = done
                if total > 0:
                    self._streams_total[si] = total

                t0 = self._streams_total[0] or 1
                t1 = self._streams_total[1] or 1
                d0 = self._streams_done[0]
                d1 = self._streams_done[1]
                w0, w1 = self._stream_weights
                combined_done  = d0 * w0 + d1 * w1
                combined_total = t0 * w0 + t1 * w1
                pct = min(88.0, (combined_done / combined_total) * 88.0) if combined_total > 0 else 0.0
            else:
                if total > 0:
                    self._last_known_total = total
                eff = total or self._last_known_total or 1
                pct = min(88.0, (done / eff) * 88.0) if done > 0 else 0.0

            self.progress = max(int(pct), self.progress)

            bps = d.get("speed") or 0.0
            if bps and bps > 0:
                self._speed_bps = bps
            self.speed = fmt_speed(self._speed_bps)

            if dual:
                remaining = max(0, (self._streams_total[0] - self._streams_done[0]) +
                                   (self._streams_total[1] - self._streams_done[1]))
            else:
                eff = total or self._last_known_total or 0
                remaining = max(0, eff - done)

            self.eta = fmt_eta(remaining / self._speed_bps) if self._speed_bps > 0 and remaining > 0 else "—"

        elif d["status"] == "finished":
            if len(self._stream_weights) == 2 and self._stream_index == 0:
                self._streams_done[0] = self._streams_total[0] or self._streams_done[0]
            else:
                self.progress = 90
                self.status   = "converting"
                self.eta      = "—"

    # ── actual download logic ───────────────────────────────────

    def run(self):
        """Blocking download — call from a thread."""
        try:
            platform = self.platform
            if platform == "yt":
                self._run_yt()
            elif platform == "tt":
                self._run_tt()
            elif platform == "ig":
                self._run_ig()
            elif platform == "sc":
                self._run_sc()
        except Exception as exc:
            log.exception("Download error in job %s", self.job_id)
            self.status = "error"
            self.error  = str(exc)

    # ── YouTube ─────────────────────────────────────────────────

    def _run_yt(self):
        out_tpl = str(DOWNLOAD_DIR / f"{self.job_id}_%(title)s.%(ext)s")

        if self.mode == "mp3":
            out_ext, mime = "mp3", "audio/mpeg"
            ydl_opts = {
                "format":          "bestaudio/best",
                "outtmpl":         out_tpl,
                "progress_hooks":  [self._hook],
                "quiet":           True,
                "no_warnings":     True,
                "postprocessors":  [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                     "preferredquality": self.quality},
                    {"key": "FFmpegMetadata"},
                    {"key": "EmbedThumbnail"},
                ],
                "writethumbnail":  True,
                "embedthumbnail":  True,
            }
        else:
            fmt_map = {
                "720":  ("bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
                         "/bestvideo[height<=720]+bestaudio/best"),
                "1080": ("bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
                         "/bestvideo[height<=1080]+bestaudio/best"),
                "best": "bestvideo+bestaudio/best",
            }
            chosen_fmt = fmt_map.get(self.quality, fmt_map["best"])
            merge_container = "mkv" if self.quality == "best" else "mp4"
            out_ext, mime = "mp4", "video/mp4"
            ydl_opts = {
                "format":              chosen_fmt,
                "outtmpl":             out_tpl,
                "progress_hooks":      [self._hook],
                "quiet":               True,
                "no_warnings":         True,
                "merge_output_format": merge_container,
                "postprocessors":      [{"key": "FFmpegMetadata"}],
            }

        # Probe
        probe_fmt = "bestvideo+bestaudio/best" if self.mode == "mp4" else "bestaudio/best"
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": probe_fmt}) as ydl:
            info = ydl.extract_info(self.url, download=False)

        self.title = info.get("title", "Unknown")

        if self.mode == "mp4":
            all_fmts   = info.get("formats") or []
            video_only = [f for f in all_fmts
                          if f.get("vcodec", "none") not in ("none", None)
                          and f.get("acodec", "none") in ("none", None)]
            if not video_only:
                video_only = [f for f in all_fmts if f.get("vcodec", "none") not in ("none", None)]

            max_height = max((f.get("height") or 0 for f in video_only), default=0)

            if self.quality == "best":
                best_vid = max(video_only,
                               key=lambda f: (f.get("height") or 0, f.get("tbr") or 0),
                               default=None)
                vcodec = (best_vid.get("vcodec") or "") if best_vid else ""
                vext   = (best_vid.get("ext") or "") if best_vid else ""
                needs_mkv = any(t in vcodec.lower() or t in vext.lower()
                                for t in ("vp9", "vp8", "av01", "av1", "webm"))
                if needs_mkv:
                    out_ext, mime = "mkv", "video/x-matroska"
                    ydl_opts["merge_output_format"] = "mkv"
                else:
                    ydl_opts["merge_output_format"] = "mp4"
                self.actual_res = f"{max_height}p" if max_height else "max"
            else:
                self.actual_res = f"{self.quality}p"

            requested = info.get("requested_formats") or []
            if len(requested) >= 2:
                v_b = requested[0].get("filesize") or requested[0].get("filesize_approx") or 0
                a_b = requested[1].get("filesize") or requested[1].get("filesize_approx") or 0
                if v_b > 0 and a_b > 0:
                    tot = v_b + a_b
                    self._stream_weights = [v_b / tot, a_b / tot]
                    self._streams_total  = [v_b, a_b]
                else:
                    self._stream_weights = [0.85, 0.15]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([self.url])

        self._finalize(out_ext, mime)

    # ── TikTok ──────────────────────────────────────────────────

    def _run_tt(self):
        out_tpl = str(DOWNLOAD_DIR / f"{self.job_id}_%(title)s.%(ext)s")

        if self.mode == "mp3":
            out_ext, mime = "mp3", "audio/mpeg"
            ydl_opts = {
                "format": "bestaudio/best", "outtmpl": out_tpl,
                "progress_hooks": [self._hook], "quiet": True, "no_warnings": True,
                "http_headers": TT_HEADERS,
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                     "preferredquality": self.quality},
                    {"key": "FFmpegMetadata"},
                ],
            }
        else:
            out_ext, mime = "mp4", "video/mp4"
            ydl_opts = {
                "format": "bestvideo+bestaudio/best", "outtmpl": out_tpl,
                "progress_hooks": [self._hook], "quiet": True, "no_warnings": True,
                "http_headers": TT_HEADERS, "merge_output_format": "mp4",
                "postprocessors": [{"key": "FFmpegMetadata"}],
            }

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                "format": "bestvideo+bestaudio/best" if self.mode == "mp4" else "bestaudio/best",
                                "http_headers": TT_HEADERS}) as ydl:
            info = ydl.extract_info(self.url, download=False)

        raw_title = info.get("title") or info.get("description") or "TikTok Video"
        self.title = raw_title[:80].strip()
        self.actual_res = ""

        if self.mode == "mp4":
            all_fmts   = info.get("formats") or []
            video_only = [f for f in all_fmts if f.get("vcodec", "none") not in ("none", None)
                          and f.get("acodec", "none") in ("none", None)]
            if not video_only:
                video_only = [f for f in all_fmts if f.get("vcodec", "none") not in ("none", None)]
            max_h = max((f.get("height") or 0 for f in video_only), default=0)
            self.actual_res = f"{max_h}p" if max_h else "best"

            requested = info.get("requested_formats") or []
            if len(requested) >= 2:
                v_b = requested[0].get("filesize") or requested[0].get("filesize_approx") or 0
                a_b = requested[1].get("filesize") or requested[1].get("filesize_approx") or 0
                if v_b > 0 and a_b > 0:
                    tot = v_b + a_b
                    self._stream_weights = [v_b / tot, a_b / tot]
                    self._streams_total  = [v_b, a_b]
                else:
                    self._stream_weights = [0.85, 0.15]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([self.url])

        self._finalize(out_ext, mime)

    # ── Instagram ───────────────────────────────────────────────

    def _run_ig(self):
        out_tpl = str(DOWNLOAD_DIR / f"{self.job_id}_%(title)s.%(ext)s")

        if self.mode == "mp3":
            out_ext, mime = "mp3", "audio/mpeg"
            ydl_opts = {
                "format": "bestaudio/best", "outtmpl": out_tpl,
                "progress_hooks": [self._hook], "quiet": True, "no_warnings": True,
                "http_headers": IG_HEADERS,
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                     "preferredquality": self.quality},
                    {"key": "FFmpegMetadata"},
                ],
            }
        else:
            out_ext, mime = "mp4", "video/mp4"
            ydl_opts = {
                "format": "bestvideo+bestaudio/best", "outtmpl": out_tpl,
                "progress_hooks": [self._hook], "quiet": True, "no_warnings": True,
                "http_headers": IG_HEADERS, "merge_output_format": "mp4",
                "postprocessors": [{"key": "FFmpegMetadata"}],
            }

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                "format": "bestvideo+bestaudio/best" if self.mode == "mp4" else "bestaudio/best",
                                "http_headers": IG_HEADERS}) as ydl:
            info = ydl.extract_info(self.url, download=False)

        self.title = info.get("title") or info.get("description") or "Instagram Video"
        self.title = self.title[:80].strip()

        if self.mode == "mp4":
            all_fmts   = info.get("formats") or []
            video_only = [f for f in all_fmts if f.get("vcodec", "none") not in ("none", None)
                          and f.get("acodec", "none") in ("none", None)]
            if not video_only:
                video_only = [f for f in all_fmts if f.get("vcodec", "none") not in ("none", None)]
            max_h = max((f.get("height") or 0 for f in video_only), default=0)
            self.actual_res = f"{max_h}p" if max_h else "best"

            requested = info.get("requested_formats") or []
            if len(requested) >= 2:
                v_b = requested[0].get("filesize") or requested[0].get("filesize_approx") or 0
                a_b = requested[1].get("filesize") or requested[1].get("filesize_approx") or 0
                if v_b > 0 and a_b > 0:
                    tot = v_b + a_b
                    self._stream_weights = [v_b / tot, a_b / tot]
                    self._streams_total  = [v_b, a_b]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([self.url])

        self._finalize(out_ext, mime)

    # ── SoundCloud ──────────────────────────────────────────────

    def _run_sc(self):
        sc_dir = DOWNLOAD_DIR / f"sc_{self.job_id}"
        sc_dir.mkdir(parents=True, exist_ok=True)
        self.sc_dir = sc_dir

        # Step 1: probe
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                "extract_flat": True}) as ydl:
            info = ydl.extract_info(self.url, download=False)

        self.title = info.get("title") or info.get("uploader") or "SoundCloud"
        entries = info.get("entries")
        track_count = len(entries) if entries else 1
        self.total_tracks = track_count

        track_hook_state = {"current": 0}

        def sc_hook(d):
            if d["status"] == "finished":
                track_hook_state["current"] += 1
                self.done_tracks = track_hook_state["current"]
                if track_count > 1:
                    self.progress = min(95, int(self.done_tracks / track_count * 95))
            elif d["status"] == "downloading":
                self.status = "downloading"
                done  = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total > 0:
                    self._last_known_total = total
                eff = total or self._last_known_total or 1
                # Per-track progress contributes fractionally to overall
                per_track_pct = (done / eff) * 95.0 if done > 0 else 0.0
                base_pct = (self.done_tracks / track_count) * 95.0 if track_count > 1 else 0.0
                per_contribution = (per_track_pct / track_count) if track_count > 1 else per_track_pct
                pct = min(95.0, base_pct + per_contribution)
                self.progress = max(int(pct), self.progress)

                bps = d.get("speed") or 0.0
                if bps > 0:
                    self._speed_bps = bps
                self.speed = fmt_speed(self._speed_bps)

                eff_total = total or self._last_known_total or 0
                remaining = max(0, eff_total - done)
                self.eta = fmt_eta(remaining / self._speed_bps) if self._speed_bps > 0 and remaining > 0 else "—"

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(sc_dir / "%(title)s.%(ext)s"),
            "progress_hooks": [sc_hook],
            "quiet": True, "no_warnings": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                 "preferredquality": self.quality},
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
            "writethumbnail": True, "embedthumbnail": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([self.url])

        mp3_files = sorted(sc_dir.glob("*.mp3"))
        if not mp3_files:
            raise FileNotFoundError("Нет MP3 после загрузки SoundCloud")

        self.files = mp3_files
        self.done_tracks = len(mp3_files)
        self.total_tracks = max(track_count, len(mp3_files))

        if len(mp3_files) == 1:
            self.file     = mp3_files[0]
            self.filename = f"{sanitize(self.title)}.mp3"
            self.mime     = "audio/mpeg"
            size_b = self.file.stat().st_size
            self.size_str = fmt_bytes(size_b)
        else:
            # Pack into a zip
            zip_path = DOWNLOAD_DIR / f"{self.job_id}_sc.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in mp3_files:
                    zf.write(f, f.name)
            self.file     = zip_path
            self.filename = f"{sanitize(self.title)}.zip"
            self.mime     = "application/zip"
            self.size_str = fmt_bytes(zip_path.stat().st_size)

        self.progress = 100
        self.status   = "done"
        self.eta      = "—"

    # ── finalize single-file jobs ────────────────────────────────

    def _finalize(self, out_ext: str, mime: str):
        search_exts = (out_ext,) if self.mode == "mp3" else (out_ext, "mkv", "mp4", "webm")
        out_file = None
        for ext in search_exts:
            candidates = list(DOWNLOAD_DIR.glob(f"{self.job_id}_*.{ext}"))
            if candidates:
                out_file = candidates[0]
                if ext != out_ext:
                    out_ext = ext
                    mime = {"mkv": "video/x-matroska", "mp4": "video/mp4",
                            "webm": "video/webm", "mp3": "audio/mpeg"}.get(ext, "application/octet-stream")
                break

        if not out_file:
            raise FileNotFoundError(f"Выходной файл не найден ({self.job_id}_*.{out_ext})")

        size_b = out_file.stat().st_size
        self.file     = out_file
        self.filename = f"{sanitize(self.title)}.{out_ext}"
        self.mime     = mime
        self.size_str = fmt_bytes(size_b)
        self.progress = 100
        self.status   = "done"
        self.eta      = "—"


# ──────────────────────────────────────────────────────────────
# BOT HANDLERS
# ──────────────────────────────────────────────────────────────

# In-memory job store: { job_id: DownloadJob }
JOBS: dict[str, DownloadJob] = {}

# Pending URL waiting for format choice: { (chat_id, msg_id): (job_id, url) }
PENDING: dict[tuple, tuple] = {}


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 *GRAB THE INTERNET*\n\n"
        "Пришли ссылку — и я скачаю:\n"
        "▸ 🎬 YouTube — видео (720p/1080p/Max) или аудио MP3\n"
        "▸ 📱 TikTok — видео или аудио\n"
        "▸ 📸 Instagram — reels/видео или аудио\n"
        "▸ 🟠 SoundCloud — треки, сеты, страницы артиста\n\n"
        "_Просто скинь ссылку ↓_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Помощь*\n\n"
        "1. Скопируй ссылку на видео/трек/плейлист\n"
        "2. Отправь её сюда\n"
        "3. Выбери формат кнопками\n"
        "4. Жди — файл придёт прямо в чат\n\n"
        "⚠️ Лимит Telegram: `50 МБ`. Тяжёлые видео могут не пройти — "
        "используй 720p или MP3 если файл слишком большой.\n\n"
        "Поддерживаемые платформы:\n"
        "`youtube.com` · `youtu.be`\n"
        "`tiktok.com` · `vm.tiktok.com`\n"
        "`instagram.com` · `instagr.am`\n"
        "`soundcloud.com`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    urls = re.findall(r'https?://\S+', text)
    if not urls:
        await update.message.reply_text("Не вижу ссылки. Пришли URL.")
        return

    url      = urls[0]
    platform = detect_platform(url)
    job_id   = uuid.uuid4().hex[:8]

    if platform == "unknown":
        await update.message.reply_text(
            "⚠️ Не поддерживаемая платформа.\n"
            "Поддерживаю: YouTube, TikTok, Instagram, SoundCloud."
        )
        return

    PENDING[(update.effective_chat.id, update.message.message_id)] = (job_id, url, platform)

    icons = {"yt": "🎬 YouTube", "tt": "📱 TikTok", "ig": "📸 Instagram", "sc": "🟠 SoundCloud"}
    prompt = f"*{icons[platform]}*\n`{url[:60]}{'…' if len(url) > 60 else ''}`\n\nВыбери формат:"

    if platform == "yt":
        kb = kb_youtube(job_id)
    elif platform in ("tt", "ig"):
        kb = kb_doom(job_id, platform)
    else:
        kb = kb_soundcloud(job_id)

    msg = await update.message.reply_text(prompt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    # Store job_id → url mapping for callback handler
    ctx.bot_data[f"url:{job_id}"]      = url
    ctx.bot_data[f"platform:{job_id}"] = platform
    ctx.bot_data[f"chat:{job_id}"]     = update.effective_chat.id
    ctx.bot_data[f"menu_msg:{job_id}"] = msg.message_id


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Cancel ──────────────────────────────────────────────────
    if data.startswith("cancel:"):
        job_id = data.split(":")[1]
        await query.edit_message_text("❌ Отменено.")
        return

    # ── Download ─────────────────────────────────────────────────
    if data.startswith("dl:"):
        _, job_id, mode, quality = data.split(":")
        url      = ctx.bot_data.get(f"url:{job_id}")
        platform = ctx.bot_data.get(f"platform:{job_id}")
        chat_id  = ctx.bot_data.get(f"chat:{job_id}")

        if not url:
            await query.edit_message_text("⚠️ Сессия устарела. Пришли ссылку заново.")
            return

        job = DownloadJob(job_id, url, platform, mode, quality)
        JOBS[job_id] = job

        # Replace the keyboard with a progress message
        await query.edit_message_text(
            f"⏳ Начинаю загрузку…\n"
            f"`{url[:55]}{'…' if len(url) > 55 else ''}`",
            parse_mode=ParseMode.MARKDOWN,
        )

        # Run download in thread pool so we don't block the event loop
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, job.run)

        # Poll loop — updates the Telegram message every 3 seconds
        msg_id = query.message.message_id
        last_pct = -1
        last_status = ""

        while True:
            await asyncio.sleep(3)

            if job.status == "error":
                await ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=f"❌ *Ошибка:*\n`{job.error}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            if job.status in ("downloading", "converting", "starting"):
                pct    = job.progress
                bar    = progress_bar(pct)
                status_label = {
                    "starting":    "Старт…",
                    "downloading": "Загрузка",
                    "converting":  "Конвертация…",
                }.get(job.status, job.status)

                title_line = f"📄 _{sanitize(job.title)}_\n" if job.title else ""

                text = (
                    f"{title_line}"
                    f"`{bar}` {pct}%\n"
                    f"*{status_label}*"
                    + (f"  ·  {job.speed}" if job.speed != "—" else "")
                    + (f"  ·  ETA {job.eta}" if job.eta != "—" else "")
                )

                if pct != last_pct or job.status != last_status:
                    try:
                        await ctx.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=text,
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass  # message unchanged — Telegram returns 400
                    last_pct    = pct
                    last_status = job.status

            elif job.status == "done":
                break

        # ── File is ready — send it ───────────────────────────────
        if not job.file or not job.file.exists():
            await ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="❌ Файл не найден после загрузки.",
            )
            return

        file_size = job.file.stat().st_size

        if file_size > TG_MAX_BYTES:
            await ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=(
                    f"⚠️ Файл слишком большой для Telegram: *{fmt_bytes(file_size)}*\n"
                    f"Лимит Bot API: 50 МБ.\n\n"
                    f"Попробуй скачать в меньшем качестве (720p или MP3)."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
            _cleanup_job(job)
            return

        # Send status: uploading
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="📤 Загружаю в Telegram…",
            )
        except Exception:
            pass

        platform_labels = {"yt": "YouTube", "tt": "TikTok", "ig": "Instagram", "sc": "SoundCloud"}
        caption = (
            f"✅ *{platform_labels.get(platform, '')}*"
            + (f" · {job.actual_res}" if job.actual_res else "")
            + f"\n📄 _{sanitize(job.title)}_"
            + f"\n💾 {job.size_str}"
        )

        with open(job.file, "rb") as fh:
            if job.mode == "mp4" and platform in ("yt", "tt", "ig"):
                await ctx.bot.send_video(
                    chat_id=chat_id,
                    video=fh,
                    filename=job.filename,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    supports_streaming=True,
                )
            elif job.mime == "audio/mpeg":
                await ctx.bot.send_audio(
                    chat_id=chat_id,
                    audio=fh,
                    filename=job.filename,
                    title=job.title or job.filename,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                # zip or mkv
                await ctx.bot.send_document(
                    chat_id=chat_id,
                    document=fh,
                    filename=job.filename,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                )

        # Delete the "uploading" status message
        try:
            await ctx.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

        # Cleanup temp files
        _cleanup_job(job)


def _cleanup_job(job: DownloadJob):
    """Remove temporary download files."""
    try:
        if job.file and job.file.exists():
            job.file.unlink(missing_ok=True)
        if job.sc_dir and job.sc_dir.exists():
            shutil.rmtree(job.sc_dir, ignore_errors=True)
        # Remove any leftover files for this job_id
        for leftover in DOWNLOAD_DIR.glob(f"{job.job_id}_*"):
            leftover.unlink(missing_ok=True)
    except Exception as exc:
        log.warning("Cleanup error: %s", exc)


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main():
    if BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
        print("\n  ⚠️  Укажи BOT_TOKEN в скрипте или через переменную окружения!")
        print("       export BOT_TOKEN=123456:ABC-...\n")
        return

    print("\n  ╔═══════════════════════════════════════════════╗")
    print("  ║  GRAB THE INTERNET — Telegram Bot             ║")
    print("  ║  YT · TikTok · Instagram · SoundCloud        ║")
    print("  ╚═══════════════════════════════════════════════╝\n")

    app = (
         Application.builder()
    .token(BOT_TOKEN)
    .base_url("http://telegram-bot-api.railway.internal:8081/bot")
    .local_mode(True)
    .read_timeout(60)
    .write_timeout(120)
    .connect_timeout(30)
    .pool_timeout(30)
    .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    print("  Бот запущен. Нажми Ctrl+C для остановки.\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
