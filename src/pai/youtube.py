"""YouTube link -> MP3 with the video thumbnail embedded as cover art (yt-dlp + ffmpeg)."""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

log = logging.getLogger(__name__)


class _YtLogger:
    """Routes yt-dlp's own output through logging. Even with quiet=True it prints
    "ERROR: ..." to stderr for failures we retry, so those land at WARNING here and
    download_mp3 reports the real outcome."""

    def debug(self, msg: str) -> None:
        log.debug(msg)

    def info(self, msg: str) -> None:
        log.debug(msg)

    def warning(self, msg: str) -> None:
        log.debug(msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg.removeprefix("ERROR: "))


URL_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?\S*v=|shorts/|live/)|youtu\.be/)"
    r"[\w-]{6,}\S*"
)
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # bot upload limit
MAX_KBPS, MIN_KBPS = 192, 64
ATTEMPTS = 3
LAME_SPEED = 7  # libmp3lame -q: 0 = slowest/best search, 9 = fastest
_BUDGET_BYTES = 47 * 1024 * 1024  # leave room for tags and cover art


class TooLong(ValueError):
    pass


@dataclass
class Track:
    path: Path
    cover: Path | None  # full-size cover art (also embedded in the mp3)
    thumb: Path | None  # small JPEG for Telegram's preview (max 320 px)
    title: str
    artist: str
    duration: int


_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{6,})")


def video_id(url: str) -> str | None:
    m = _ID_RE.search(url)
    return m.group(1) if m else None


def find_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def bitrate_for(duration_s: int) -> int:
    """Highest bitrate (kbps) that keeps the file under Telegram's limit."""
    if duration_s <= 0:
        return MAX_KBPS
    kbps = int(_BUDGET_BYTES * 8 / duration_s / 1000)
    kbps = min(MAX_KBPS, kbps - kbps % 32 if kbps >= 96 else kbps)
    if kbps < MIN_KBPS:
        minutes = int(_BUDGET_BYTES * 8 / MIN_KBPS / 1000 / 60)
        raise TooLong(
            f"that's {duration_s // 60} min; I can only fit about {minutes} min under 50 MB"
        )
    return kbps


def download_mp3(url: str, out_dir: Path, on_stage: Callable[[str], None] | None = None) -> Track:
    """Blocking: run it in a thread. Downloads only the single video, never a playlist.

    `on_stage` gets "converting" once the audio is downloaded, for progress messages.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    base = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "logger": _YtLogger(),
    }

    def stage(d: dict) -> None:
        if on_stage and d.get("status") == "finished":
            on_stage("converting")

    with yt_dlp.YoutubeDL(base) as probe:
        info = probe.extract_info(url, download=False)
    if info.get("is_live"):
        raise ValueError("that's a live stream; send it again once it has ended")
    duration = int(info.get("duration") or 0)
    kbps = bitrate_for(duration)

    opts = {
        **base,
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "writethumbnail": True,
        "http_chunk_size": 10 * 1024 * 1024,
        "progress_hooks": [stage],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(kbps)},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": True},
        ],
        # LAME's faster search: about twice the encode speed, same bitrate and size.
        "postprocessor_args": {"extractaudio+ffmpeg_o": ["-compression_level", str(LAME_SPEED)]},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                # Reuse the probe's extraction instead of resolving the video twice.
                info = ydl.process_ie_result(info, download=True)
                break
            except yt_dlp.utils.DownloadError as e:
                # YouTube hands out the odd 403 on a media URL; fresh URLs usually work.
                if "403" not in str(e) or attempt == ATTEMPTS:
                    raise
                log.info("youtube 403 on attempt %d for %s; retrying with fresh URLs", attempt, url)
                info = ydl.extract_info(url, download=False)
    vid = info["id"]
    mp3 = out_dir / f"{vid}.mp3"
    cover = out_dir / f"{vid}.jpg"
    if not mp3.exists():
        raise RuntimeError("conversion produced no mp3 (is ffmpeg installed?)")
    if mp3.stat().st_size > TELEGRAM_MAX_BYTES:
        raise TooLong("the mp3 came out over Telegram's 50 MB limit")
    cover = cover if cover.exists() else None
    log.info("youtube mp3 ready: %s (%ds, %d kbps)", info.get("title") or vid, duration, kbps)
    return Track(
        path=mp3,
        cover=cover,
        thumb=_small_thumb(cover) if cover else None,
        title=info.get("track") or info.get("title") or vid,
        artist=info.get("artist") or info.get("uploader") or "",
        duration=int(info.get("duration") or duration),
    )


def _small_thumb(cover: Path) -> Path | None:
    thumb = cover.with_name(cover.stem + "-thumb.jpg")
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(cover),
         "-vf", "scale='min(320,iw)':-2", "-q:v", "5", str(thumb)],
        capture_output=True,
    )  # fmt: skip
    return thumb if result.returncode == 0 and thumb.exists() else None


def safe_filename(title: str) -> str:
    name = " ".join(re.sub(r'[\\/:*?"<>|]+', " ", title).split())[:80] or "audio"
    return f"{name}.mp3"
