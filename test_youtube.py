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
    _fit_keep,
    _resolve_section,
    _trim_mp3,
    build_yt_dlp_args,
    duration_label,
    parse_timestamp,
)


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {name} {detail}")
    print(f"OK {name}")


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


def test_command_matches_usual_flags() -> None:
    command = build_yt_dlp_args(
        ["yt-dlp"],
        "https://www.youtube.com/watch?v=pfx4r7_WdP8",
        "1:12:00",
        "1:13:00",
        Path(r"D:\ytmusic"),
        Path.home() / "Downloads" / "cookies.txt",
    )
    text = " ".join(command)
    check("uses download section", "--download-sections" in command and "*1:12:00-1:13:00" in command)
    check("forces keyframes", "--force-keyframes-at-cuts" in command)
    check("extracts mp3", "-x" in command and "mp3" in command)
    check("saves to chosen folder", "-P" in command and r"D:\ytmusic" in text)
    check("uses cookies", "--cookies" in command)


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
    test_times()
    test_command_matches_usual_flags()
    test_trim_to_requested_length()
    print("All youtube checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
