# -*- coding: utf-8 -*-
"""Kiểm tra phần cắt thời gian và lệnh yt-dlp, không tải mạng. Chạy: python test_youtube.py"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from processor import AudioError, ffmpeg_available
from youtube_clip import (
    _blocked_dir,
    _fit_keep,
    _resolve_section,
    _trim_mp3,
    build_yt_dlp_args,
    duration_label,
    ffmpeg_for_sections,
    parse_timestamp,
    yt_dlp_command,
    _score_ffmpeg_version,
)


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {name} {detail}")
    print(f"OK {name}")


def test_yt_dlp_comes_from_python() -> None:
    import os

    windir = os.environ.get("WINDIR")
    if windir:
        broken = Path(windir) / "System32" / "yt-dlp.exe"
        check("ignore Windows yt-dlp.exe", _blocked_dir(broken), str(broken))
    command = yt_dlp_command()
    check(
        "uses the project Python package",
        command == [sys.executable, "-m", "yt_dlp"],
        str(command),
    )


def test_times() -> None:
    check("parse 1:12:00", parse_timestamp("1:12:00") == 1 * 3600 + 12 * 60)
    check("one minute label", duration_label(60) == "1min")
    check("thirty second label", duration_label(30) == "30s")
    start, end, start_sec, end_sec = _resolve_section("1:12:00", "1:13:00", 30, 0)
    check("section stays one minute", end_sec - start_sec == 60 and start == "1:12:00" and end == "1:13:00")
    try:
        _resolve_section("1:12:00", "1:13:00", 90, 0)
    except AudioError:
        check("reject keep longer than section", True)
    else:
        check("reject keep longer than section", False)
    try:
        parse_timestamp("https://evil.example")
    except AudioError:
        check("reject bogus time", True)
    else:
        check("reject bogus time", False)


def test_ffmpeg_rank() -> None:
    nightly = _score_ffmpeg_version("ffmpeg version N-125875-g5d4d3bdc61-20260731 Copyright")
    current = _score_ffmpeg_version("ffmpeg version 9.0.2 Copyright")
    broken = _score_ffmpeg_version("ffmpeg version 8.1 Copyright")
    check("prefer ffmpeg 9 over the July nightly", current > nightly, f"{current} {nightly}")
    check("prefer ffmpeg 9 over 8.1", current > broken)
    chosen = ffmpeg_for_sections()
    check("finds an ffmpeg", bool(chosen), str(chosen))
    if chosen:
        check("chosen ffmpeg is not the old nightly", "20260731" not in chosen, chosen)


def test_command_matches_usual_flags() -> None:
    command = build_yt_dlp_args(
        ["yt-dlp"],
        "https://www.youtube.com/watch?v=pfx4r7_WdP8",
        "1:12:00",
        "1:13:00",
        Path(r"D:\ytmusic"),
        Path.home() / "Downloads" / "cookies.txt",
        ffmpeg=r"C:\ffmpeg\bin\ffmpeg.exe",
    )
    text = " ".join(command)
    check("uses download section", "--download-sections" in command and "*1:12:00-1:13:00" in command)
    check("forces keyframes", "--force-keyframes-at-cuts" in command)
    check("extracts mp3", "-x" in command and "mp3" in command)
    check("saves to chosen folder", "-P" in command and r"D:\ytmusic" in text)
    check("uses cookies", "--cookies" in command)
    check("prefers https streams", "-S" in command and "proto:https" in command)
    check("pins ffmpeg", "--ffmpeg-location" in command and r"C:\ffmpeg\bin\ffmpeg.exe" in command)
    check("skips web creator when cookies are sent", "player_client=default,-web_creator" in text)


def test_trim_to_requested_length() -> None:
    if not ffmpeg_available():
        print("SKIP trim, ffmpeg missing")
        return
    ffmpeg = shutil.which("ffmpeg")
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "minute.mp3"
        dest = Path(directory) / "half.mp3"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-q:a",
                "4",
                str(source),
            ],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        offset, keep = _fit_keep(2.0, 0.5, 0.2)
        _trim_mp3(source, dest, offset, keep)
        import soundfile as sf

        duration = sf.info(dest).duration
    check("trimmed near half second", abs(duration - 0.5) < 0.15, f"{duration:.3f}")


def main() -> int:
    test_yt_dlp_comes_from_python()
    test_times()
    test_ffmpeg_rank()
    test_command_matches_usual_flags()
    test_trim_to_requested_length()
    print("All youtube checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
