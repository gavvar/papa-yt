# -*- coding: utf-8 -*-
r"""Tải một khoảng từ YouTube rồi lưu MP3 vào thư mục chọn.

Lệnh gốc anh đang dùng:

    yt-dlp --cookies "$HOME\Downloads\cookies.txt" --force-keyframes-at-cuts ^
        --download-sections "*1:12:00-1:13:00" -x --audio-format mp3 ^
        -P "D:\ytmusic" -o "%(title)s - 1min.%(ext)s" "URL"

Công cụ này gọi đúng các cờ đó. Nếu đoạn tải về dài hơn phần cần giữ
(ví dụ tải 1 phút nhưng chỉ lấy 30 giây), FFmpeg cắt lại đúng số giây.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import soundfile as sf

from processor import AudioError, ffmpeg_available

MAX_SECTION_SEC = 2 * 60 * 60
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}


@dataclass
class ClipResult:
    path: Path
    title: str
    duration_sec: float
    section_start: str
    section_end: str
    keep_seconds: float | None
    offset_seconds: float

    def metadata(self) -> dict:
        return {
            "kind": "youtube",
            "saved_path": str(self.path),
            "title": self.title,
            "duration_out": round(float(self.duration_sec), 3),
            "section_start": self.section_start,
            "section_end": self.section_end,
            "keep_seconds": None if self.keep_seconds is None else round(float(self.keep_seconds), 3),
            "offset_seconds": round(float(self.offset_seconds), 3),
        }


def default_cookies_path() -> Path:
    return Path.home() / "Downloads" / "cookies.txt"


def default_output_dir() -> str:
    return r"D:\ytmusic"


def choose_output_dir(initial: str = "") -> str:
    """Mở hộp thoại chọn thư mục của Windows. Chuỗi rỗng nghĩa là anh bấm hủy."""
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "Chon thu muc luu MP3"
$dialog.ShowNewFolderButton = $true
try { $dialog.UseDescriptionForTitle = $true } catch {}
if ($env:INITIAL_DIR -and (Test-Path -LiteralPath $env:INITIAL_DIR)) {
  $dialog.SelectedPath = $env:INITIAL_DIR
}
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.StartPosition = "CenterScreen"
[void]$owner.Show()
$owner.Activate()
$result = $dialog.ShowDialog($owner)
$owner.Close()
$owner.Dispose()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::Out.WriteLine($dialog.SelectedPath)
}
"""
    env = os.environ.copy()
    env["INITIAL_DIR"] = initial or ""
    run_kwargs: dict = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env,
        "timeout": 180,
        "check": False,
    }
    if sys.platform == "win32":
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        **run_kwargs,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        raise AudioError(detail[-1] if detail else "Không mở được hộp chọn thư mục.")
    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def yt_dlp_command() -> list[str] | None:
    """Dùng gói pip cùng Python của app.

    Bản yt-dlp.exe trong C:\\WINDOWS\\system32 hay hỏng kho lưu của PyInstaller
    (PYI-24112) nên không được ưu tiên.
    """
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        found = shutil.which("yt-dlp")
        if found and not _blocked_dir(Path(found)):
            return [found]
        return None
    return [sys.executable, "-m", "yt_dlp"]


def yt_dlp_available() -> bool:
    return yt_dlp_command() is not None


def youtube_defaults() -> dict:
    cookies = default_cookies_path()
    return {
        "yt_dlp": yt_dlp_available(),
        "ffmpeg": ffmpeg_available(),
        "cookies": str(cookies),
        "cookies_found": cookies.is_file(),
        "output_dir": default_output_dir(),
        "max_section_sec": MAX_SECTION_SEC,
    }


def download_section(
    url: str,
    start: str,
    output_dir: str | Path,
    end: str | None = None,
    keep_seconds: float | None = None,
    offset_seconds: float = 0.0,
    cookies: str | Path | None = None,
) -> ClipResult:
    """Tải khoảng start-end, rồi cắt còn keep_seconds nếu anh chỉ muốn một phần."""
    page = _require_youtube_url(url)
    start_text, end_text, start_sec, end_sec = _resolve_section(start, end, keep_seconds, offset_seconds)
    folder = _require_output_dir(output_dir)
    cookie_file = _resolve_cookies(cookies)
    command_base = yt_dlp_command()
    if command_base is None:
        found = shutil.which("yt-dlp")
        if found and _blocked_dir(Path(found)):
            raise AudioError(
                "File yt-dlp trong thư mục Windows bị hỏng nên không dùng. "
                f"Đổi tên hoặc xóa {found}, rồi chạy lại run.bat."
            )
        raise AudioError(
            "Chưa thấy yt-dlp. Chạy lại run.bat, hoặc: .venv\\Scripts\\python.exe -m pip install -r requirements.txt"
        )
    if not ffmpeg_available():
        raise AudioError("Tải audio từ YouTube cần FFmpeg trong PATH.")

    label = duration_label(keep_seconds if keep_seconds is not None else end_sec - start_sec)
    with tempfile.TemporaryDirectory(prefix="ytclip-") as temporary:
        source = _run_yt_dlp(
            command_base,
            page,
            start_text,
            end_text,
            Path(temporary),
            cookie_file,
        )
        duration = _media_duration(source)
        title = _safe_stem(source.stem)
        offset, keep = _fit_keep(duration, keep_seconds, offset_seconds)
        destination = _unique_path(folder / f"{title} - {label}.mp3")
        if keep is None:
            shutil.move(str(source), destination)
        else:
            _trim_mp3(source, destination, offset, keep)
        saved_duration = _media_duration(destination)

    return ClipResult(
        path=destination,
        title=title,
        duration_sec=saved_duration,
        section_start=start_text,
        section_end=end_text,
        keep_seconds=keep,
        offset_seconds=offset if keep is not None else 0.0,
    )


def parse_timestamp(value: str) -> float:
    text = (value or "").strip()
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?", text):
        raise AudioError("Thời gian phải dạng giây, phút:giây hoặc giờ:phút:giây. Ví dụ 1:12:00.")
    parts = text.split(":")
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    hours, minutes, seconds = parts
    if int(minutes) > 59 or float(seconds) >= 60:
        raise AudioError("Phút và giây trong dạng giờ:phút:giây phải nhỏ hơn 60.")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def format_timestamp(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def duration_label(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60 and secs == 0:
        return f"{minutes}min"
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    if minutes == 0 and secs == 0:
        return f"{hours}h"
    return f"{hours}h{minutes:02d}m"


def build_yt_dlp_args(
    executable: list[str],
    url: str,
    start: str,
    end: str,
    output_dir: Path,
    cookies: Path | None,
) -> list[str]:
    command = [
        *executable,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--force-keyframes-at-cuts",
        "--download-sections",
        f"*{start}-{end}",
        "-x",
        "--audio-format",
        "mp3",
        "-P",
        str(output_dir),
        "-o",
        "%(title).180B.%(ext)s",
        "--print",
        "after_move:filepath",
    ]
    if sys.platform == "win32":
        command.extend(["--windows-filenames"])
    if cookies is not None:
        command.extend(["--cookies", str(cookies)])
    command.append(url)
    return command


def _resolve_section(
    start: str,
    end: str | None,
    keep_seconds: float | None,
    offset_seconds: float,
) -> tuple[str, str, float, float]:
    start_sec = parse_timestamp(start)
    offset = float(offset_seconds or 0)
    if offset < 0:
        raise AudioError("Điểm bắt đầu của phần giữ lại phải từ 0 trở lên.")
    if keep_seconds is not None and keep_seconds <= 0:
        raise AudioError("Số giây muốn giữ phải lớn hơn 0.")
    if end and end.strip():
        end_sec = parse_timestamp(end)
        end_text = end.strip()
    elif keep_seconds is not None:
        end_sec = start_sec + offset + float(keep_seconds)
        end_text = format_timestamp(end_sec)
    else:
        raise AudioError("Hãy nhập thời điểm kết thúc, hoặc nhập số giây muốn giữ.")
    if end_sec <= start_sec:
        raise AudioError("Thời điểm kết thúc phải sau thời điểm bắt đầu.")
    section = end_sec - start_sec
    if section > MAX_SECTION_SEC:
        raise AudioError("Mỗi lần chỉ tải tối đa 2 giờ.")
    if keep_seconds is not None and offset + float(keep_seconds) > section + 0.05:
        raise AudioError("Phần muốn giữ nằm ngoài khoảng thời gian đã chọn.")
    return start.strip(), end_text, start_sec, end_sec


def _require_youtube_url(url: str) -> str:
    text = (url or "").strip()
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in YOUTUBE_HOSTS:
        raise AudioError("Chỉ nhận link https của YouTube.")
    if not parsed.path and not parsed.query:
        raise AudioError("Link YouTube chưa đủ.")
    return text


def _require_output_dir(output_dir: str | Path) -> Path:
    text = str(output_dir or "").strip().strip('"')
    if not text:
        raise AudioError("Hãy chọn thư mục lưu file.")
    folder = Path(text).expanduser()
    if not folder.is_absolute():
        folder = Path.cwd() / folder
    folder = folder.resolve()
    if folder.exists() and not folder.is_dir():
        raise AudioError("Đường dẫn lưu đang là một file, không phải thư mục.")
    blocked = _blocked_dir(folder)
    if blocked:
        raise AudioError(f"Không lưu vào thư mục hệ thống: {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _blocked_dir(folder: Path) -> bool:
    windir = os.environ.get("WINDIR")
    if not windir:
        return False
    windows = os.path.normcase(str(Path(windir).resolve()))
    target = os.path.normcase(str(Path(folder).resolve()))
    return target == windows or target.startswith(windows + os.sep)


def _resolve_cookies(cookies: str | Path | None) -> Path | None:
    if cookies is None or str(cookies).strip() == "":
        found = default_cookies_path()
        return found if found.is_file() else None
    path = Path(str(cookies).strip().strip('"')).expanduser()
    if not path.is_file():
        raise AudioError(f"Không thấy file cookies: {path}")
    return path


def _run_yt_dlp(
    executable: list[str],
    url: str,
    start: str,
    end: str,
    output_dir: Path,
    cookies: Path | None,
) -> Path:
    command = build_yt_dlp_args(executable, url, start, end, output_dir, cookies)
    completed = _run(command, timeout=_timeout_for(start, end))
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        last_line = detail.splitlines()[-1][:300] if detail else "yt-dlp dừng với lỗi."
        raise AudioError(f"Không tải được đoạn YouTube. {last_line}")
    printed = [
        line.strip()
        for line in completed.stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    if printed:
        candidate = Path(printed[-1].strip('"'))
        if candidate.is_file():
            return candidate
    matches = sorted(output_dir.glob("*.mp3"), key=lambda item: item.stat().st_mtime)
    if not matches:
        raise AudioError("yt-dlp chạy xong nhưng không thấy file MP3.")
    return matches[-1]


def _timeout_for(start: str, end: str) -> int:
    span = max(30.0, parse_timestamp(end) - parse_timestamp(start))
    return int(min(3600, max(180, span * 4 + 90)))


def _fit_keep(
    duration: float,
    keep_seconds: float | None,
    offset_seconds: float,
) -> tuple[float, float | None]:
    offset = float(offset_seconds or 0)
    if keep_seconds is None and offset <= 0.05:
        return 0.0, None
    if offset >= duration - 0.05:
        raise AudioError("Điểm bắt đầu giữ nằm ngoài đoạn vừa tải.")
    available = duration - offset
    if keep_seconds is None:
        keep = available
    else:
        keep = float(keep_seconds)
        if keep > available + 0.4:
            raise AudioError(
                f"Đoạn tải về còn {available:.1f} giây từ mốc đã chọn, "
                f"ngắn hơn {keep:.1f} giây muốn giữ."
            )
        keep = min(keep, available)
    if offset <= 0.05 and abs(keep - duration) <= 0.4:
        return 0.0, None
    return offset, keep


def _trim_mp3(source: Path, dest: Path, offset: float, keep: float) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioError("Cắt độ dài cần FFmpeg trong PATH.")
    command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ss",
        f"{offset:.3f}",
        "-t",
        f"{keep:.3f}",
        "-vn",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(dest),
    ]
    completed = _run(command, timeout=180)
    if completed.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        last_line = detail.splitlines()[-1][:300] if detail else ""
        raise AudioError(f"Không cắt được độ dài mong muốn. {last_line}".strip())


def _media_duration(path: Path) -> float:
    try:
        return float(sf.info(path).duration)
    except Exception as exc:
        raise AudioError(f"Không đọc được thời lượng của {path.name}.") from exc


def _safe_stem(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120] or "youtube"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 100):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise AudioError("Thư mục đã có quá nhiều file trùng tên.")


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    kwargs: dict = {"capture_output": True, "timeout": timeout, "check": False}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise AudioError("Tải hoặc cắt audio quá lâu và đã bị dừng.") from exc
