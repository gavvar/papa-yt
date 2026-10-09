# -*- coding: utf-8 -*-
r"""Audio Data Augmentation — lõi xử lý tín hiệu.

Pipeline, theo đúng thứ tự:
    1. Pitch shift        −1.5 .. +1.5 semitone (mặc định 0, giữ nguyên tone)
    2. Time stretch       0.95x .. 1.05x        (mặc định 1.00x)
    3. High-pass          cắt dưới 70 Hz
    4. Low-pass           cắt trên 15 kHz
    5. Noise injector     pink/white, mix −48 dB so với RMS của tín hiệu

Cài đặt thư viện (chạy trong thư mục dự án)
------------------------------------------
Cần Python 3.10 trở lên.

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt

WAV, FLAC, OGG và phần lớn MP3 đọc trực tiếp.
M4A và AAC giải mã bằng FFmpeg có sẵn trong PATH.
Xuất MP3 cũng dùng FFmpeg đó. Nếu terminal mới không thấy ffmpeg:

    winget install Gyan.FFmpeg

Tải tay: https://ffmpeg.org/download.html

Nếu PowerShell chặn kích hoạt venv, không cần Activate.ps1.
Gọi thẳng .venv\Scripts\python.exe như các lệnh ở trên.

Chạy công cụ
------------
    .venv\Scripts\python.exe api.py
        Giao diện:    http://127.0.0.1:8000
        Tài liệu API: http://127.0.0.1:8000/docs

    .venv\Scripts\python.exe cli.py input.wav -o output.wav
    .venv\Scripts\python.exe cli.py --help

Thư viện dùng trong pipeline
----------------------------
    librosa   pitch shift và time stretch (phase vocoder)
    scipy     high-pass / low-pass Butterworth, lọc zero-phase
    numpy     nhiễu trắng, nhiễu hồng, trộn gain
    soundfile ghi WAV PCM 16-bit
    pydub     xuất MP3 (ủy quyền cho FFmpeg)
"""

from __future__ import annotations

import io
import math
import os
import random
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import warnings
from dataclasses import dataclass

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt, sosfiltfilt

__version__ = "1.0.0"

PITCH_MIN = -1.5
PITCH_MAX = 1.5
SPEED_MIN = 0.95
SPEED_MAX = 1.05
HIGHPASS_MIN = 20.0
HIGHPASS_MAX = 500.0
LOWPASS_MIN = 4000.0
LOWPASS_MAX = 20000.0
NOISE_DB_MIN = -80.0
NOISE_DB_MAX = -24.0
DEFAULT_HIGHPASS_HZ = 70.0
DEFAULT_LOWPASS_HZ = 15000.0
DEFAULT_NOISE_DB = -48.0

MIN_SAMPLE_RATE = 8000
MIN_DURATION_SEC = 0.3
MAX_DURATION_SEC = 600.0
MAX_CHANNELS = 8

AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".aiff", ".aif", ".m4a", ".aac"}

# Một nguồn số cho API, CLI và giao diện. Mặc định cố ý nhẹ để giữ tiếng tự nhiên.
SPEC = {
    "pitch": {
        "default": 0.0,
        "min": PITCH_MIN,
        "max": PITCH_MAX,
        "step": 0.1,
    },
    "speed": {
        "default": 1.0,
        "min": SPEED_MIN,
        "max": SPEED_MAX,
        "step": 0.01,
    },
    "highpass_hz": {
        "default": DEFAULT_HIGHPASS_HZ,
        "min": HIGHPASS_MIN,
        "max": HIGHPASS_MAX,
        "step": 1,
    },
    "lowpass_hz": {
        "default": DEFAULT_LOWPASS_HZ,
        "min": LOWPASS_MIN,
        "max": LOWPASS_MAX,
        "step": 50,
    },
    "noise_db": {
        "default": DEFAULT_NOISE_DB,
        "min": NOISE_DB_MIN,
        "max": NOISE_DB_MAX,
        "step": 1,
    },
    "noise_type": {"default": "pink", "choices": ["pink", "white", "none"]},
    "output_format": {"default": "wav", "choices": ["wav", "mp3"]},
    "eq": {"default": True},
}

_PROCESS_LOCK = threading.Lock()

# librosa 1.0 vẫn tự truyền hop_length/n_fft vào phase vocoder dù đã đánh dấu bỏ.
warnings.filterwarnings("ignore", message=r"The `(hop_length|n_fft)` parameter is deprecated", category=FutureWarning)


class AudioError(Exception):
    """Lỗi tham số hoặc file mà người dùng có thể sửa được."""


@dataclass
class AugmentConfig:
    pitch_semitones: float = 0.0
    speed: float = 1.0
    highpass_hz: float = DEFAULT_HIGHPASS_HZ
    lowpass_hz: float = DEFAULT_LOWPASS_HZ
    eq: bool = True
    noise_type: str = "pink"
    noise_db: float = DEFAULT_NOISE_DB
    seed: int | None = None
    randomize: bool = False


@dataclass
class AugmentResult:
    audio: np.ndarray
    sample_rate: int
    pitch_semitones: float
    speed: float
    highpass_hz: float | None
    lowpass_hz: float | None
    eq_clamped: bool
    noise_type: str
    noise_db: float | None
    seed: int | None
    duration_in: float
    duration_out: float
    channels: int


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def get_capabilities() -> dict:
    return {
        "ffmpeg": ffmpeg_available(),
        "version": __version__,
        "spec": SPEC,
        "input_extensions": sorted(AUDIO_EXTENSIONS),
        "output_formats": ["wav", "mp3"],
        "max_duration_sec": MAX_DURATION_SEC,
        "max_upload_mb": 40,
        "pipeline": ["pitch_shift", "time_stretch", "highpass", "lowpass", "noise"],
    }


def load_audio(path: str) -> tuple[np.ndarray, int]:
    """Đọc file thành mảng float32 dạng (kênh, mẫu), giữ nguyên sample rate.

    WAV, FLAC, OGG và nhiều MP3 đi qua soundfile.
    M4A, AAC và file soundfile từ chối được FFmpeg trên máy giải mã ra WAV tạm.
    """
    try:
        audio, sample_rate = _read_soundfile(path)
    except AudioError:
        raise
    except Exception as direct_error:
        audio, sample_rate = _read_via_ffmpeg(path, direct_error)

    audio = _as_channels(np.asarray(audio, dtype=np.float32))
    if audio.size == 0 or audio.shape[-1] == 0:
        raise AudioError("File không có mẫu âm thanh.")
    return audio, int(sample_rate)


def _read_soundfile(path: str) -> tuple[np.ndarray, int]:
    data, sample_rate = sf.read(path, always_2d=True, dtype="float32")
    if data.size == 0:
        raise AudioError("File không có mẫu âm thanh.")
    return np.ascontiguousarray(data.T), int(sample_rate)


def _read_via_ffmpeg(path: str, direct_error: Exception) -> tuple[np.ndarray, int]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioError(
            "Không mở được file bằng bộ đọc WAV/FLAC/MP3, và không thấy FFmpeg trong PATH. "
            "Cài FFmpeg rồi mở lại terminal: winget install Gyan.FFmpeg"
        ) from direct_error

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            temp_path = handle.name
        command = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            path,
            "-vn",
            "-acodec",
            "pcm_s16le",
            temp_path,
        ]
        run_kwargs: dict = {"capture_output": True, "timeout": 180, "check": False}
        if sys.platform == "win32":
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            completed = subprocess.run(command, **run_kwargs)
        except subprocess.TimeoutExpired as exc:
            raise AudioError("FFmpeg giải mã quá lâu và đã bị dừng.") from exc

        if completed.returncode != 0 or not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            last_line = detail.splitlines()[-1][:300] if detail else ""
            message = "FFmpeg có trên máy nhưng không giải mã được file này."
            if last_line:
                message = f"{message} {last_line}"
            raise AudioError(message) from direct_error
        return _read_soundfile(temp_path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def make_demo_tone(sample_rate: int = 44100, seconds: float = 4.0) -> tuple[np.ndarray, int]:
    """Đoạn mẫu có tiếng trầm 55 Hz để high-pass 70 Hz tạo khác biệt nghe được."""
    count = int(sample_rate * seconds)
    time = np.arange(count, dtype=np.float64) / sample_rate
    left = (
        0.28 * np.sin(2 * np.pi * 55 * time)
        + 0.22 * np.sin(2 * np.pi * 220 * time)
        + 0.12 * np.sin(2 * np.pi * 440 * time)
        + 0.06 * np.sin(2 * np.pi * 880 * time)
    )
    right = (
        0.28 * np.sin(2 * np.pi * 55 * time)
        + 0.18 * np.sin(2 * np.pi * 223 * time)
        + 0.1 * np.sin(2 * np.pi * 660 * time)
    )
    phrase = 0.7 + 0.3 * np.sin(2 * np.pi * 0.5 * time)
    attack = np.clip(time / 0.03, 0, 1)
    release = np.clip((seconds - time) / 0.08, 0, 1)
    envelope = (phrase * attack * release).astype(np.float32)
    audio = np.vstack(
        [
            (left * envelope).astype(np.float32),
            (right * envelope).astype(np.float32),
        ]
    )
    return np.ascontiguousarray(audio * np.float32(0.7), dtype=np.float32), sample_rate


def augment(
    audio: np.ndarray,
    sample_rate: int,
    config: AugmentConfig | None = None,
) -> AugmentResult:
    """Chạy toàn bộ pipeline. An toàn khi API xử lý song song nhờ một khóa toàn cục."""
    with _PROCESS_LOCK:
        return _augment_unlocked(audio, sample_rate, config or AugmentConfig())


def augment_file(path: str, config: AugmentConfig | None = None) -> AugmentResult:
    audio, sample_rate = load_audio(path)
    return augment(audio, sample_rate, config)


def ensure_output_format(fmt: str) -> str:
    normalized = fmt.lower().lstrip(".")
    if normalized not in {"wav", "mp3"}:
        raise AudioError("Chỉ xuất được WAV hoặc MP3.")
    if normalized == "mp3" and not ffmpeg_available():
        raise AudioError(
            "Xuất MP3 cần FFmpeg trong PATH. Cài bằng: winget install Gyan.FFmpeg. "
            "Hoặc chọn WAV."
        )
    return normalized


def audio_to_bytes(audio: np.ndarray, sample_rate: int, fmt: str) -> bytes:
    fmt = ensure_output_format(fmt)
    wav_buffer = io.BytesIO()
    sf.write(wav_buffer, _interleaved(audio), sample_rate, format="WAV", subtype="PCM_16")
    wav_bytes = wav_buffer.getvalue()
    if fmt == "wav":
        return wav_bytes
    return _wav_bytes_to_mp3(wav_bytes)


def clip_audio_bytes(payload: bytes, suffix: str, seconds: float) -> tuple[bytes, str]:
    """Cắt `seconds` giây đầu của file kết quả để tải về."""
    if not math.isfinite(seconds) or seconds <= 0:
        raise AudioError("Số giây muốn tải phải lớn hơn 0.")
    if seconds > MAX_DURATION_SEC:
        raise AudioError("Số giây muốn tải dài hơn 10 phút.")
    normalized = suffix.lower()
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    if normalized not in {".wav", ".mp3"}:
        normalized = ".wav"
    if normalized == ".wav":
        data, sample_rate = sf.read(io.BytesIO(payload), always_2d=True, dtype="float32")
        frames = min(len(data), max(1, int(round(seconds * sample_rate))))
        buffer = io.BytesIO()
        sf.write(buffer, data[:frames], sample_rate, format="WAV", subtype="PCM_16")
        return buffer.getvalue(), ".wav"

    ensure_output_format("mp3")
    source_path = None
    dest_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as source:
            source.write(payload)
            source_path = source.name
        dest_path = f"{source_path}.clip.mp3"
        command = [
            shutil.which("ffmpeg"),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            source_path,
            "-t",
            f"{seconds:.3f}",
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            dest_path,
        ]
        run_kwargs: dict = {"capture_output": True, "timeout": 180, "check": False}
        if sys.platform == "win32":
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        completed = subprocess.run(command, **run_kwargs)
        if completed.returncode != 0 or not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            last_line = detail.splitlines()[-1][:300] if detail else ""
            raise AudioError(f"Không cắt được số giây muốn tải. {last_line}".strip())
        return Path(dest_path).read_bytes(), ".mp3"
    finally:
        for path in (source_path, dest_path):
            if path and os.path.exists(path):
                os.remove(path)


def write_audio(path: str, audio: np.ndarray, sample_rate: int, fmt: str | None = None) -> str:
    from pathlib import Path

    destination = Path(path)
    resolved = ensure_output_format(fmt or destination.suffix.lstrip(".") or "wav")
    destination = destination.with_suffix(f".{resolved}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(audio_to_bytes(audio, sample_rate, resolved))
    return str(destination)


def format_summary(result: AugmentResult) -> str:
    if result.highpass_hz is None or result.lowpass_hz is None:
        equalizer = "EQ tắt"
    else:
        equalizer = f"EQ {result.highpass_hz:.0f}–{result.lowpass_hz:.0f} Hz"
        if result.eq_clamped:
            equalizer += " (đã hạ theo Nyquist)"
    if result.noise_type == "none" or result.noise_db is None:
        noise = "không nhiễu"
    else:
        kind = "nhiễu hồng" if result.noise_type == "pink" else "nhiễu trắng"
        noise = f"{kind} {result.noise_db:.0f} dB"
    seed = f" | seed {result.seed}" if result.seed is not None else ""
    return (
        f"pitch {result.pitch_semitones:+.2f} st | speed {result.speed:.3f}x | "
        f"{equalizer} | {noise}{seed}\n"
        f"{result.duration_in:.2f}s -> {result.duration_out:.2f}s | "
        f"{result.sample_rate} Hz | {result.channels} kenh"
    )


def result_metadata(result: AugmentResult) -> dict:
    return {
        "pitch_semitones": round(float(result.pitch_semitones), 4),
        "speed": round(float(result.speed), 4),
        "highpass_hz": None if result.highpass_hz is None else round(float(result.highpass_hz), 2),
        "lowpass_hz": None if result.lowpass_hz is None else round(float(result.lowpass_hz), 2),
        "eq": result.highpass_hz is not None,
        "eq_clamped": bool(result.eq_clamped),
        "noise_type": result.noise_type,
        "noise_db": None if result.noise_db is None else round(float(result.noise_db), 2),
        "seed": result.seed,
        "sample_rate": int(result.sample_rate),
        "channels": int(result.channels),
        "duration_in": round(float(result.duration_in), 3),
        "duration_out": round(float(result.duration_out), 3),
    }


def _augment_unlocked(
    audio: np.ndarray,
    sample_rate: int,
    config: AugmentConfig,
) -> AugmentResult:
    prepared = _prepare_audio(audio)
    rate = int(sample_rate)
    _validate_audio(prepared, rate)
    _validate_config(config)
    duration_in = prepared.shape[-1] / rate
    rng, seed = _make_rng(config)
    pitch, speed = _resolve_motion(config, rng)

    processed = prepared
    if abs(pitch) >= 1e-4:
        processed = _pitch_shift(processed, rate, pitch)
    if abs(speed - 1.0) >= 1e-4:
        processed = _time_stretch(processed, speed)

    highpass = None
    lowpass = None
    clamped = False
    if config.eq:
        processed, highpass, lowpass, clamped = _apply_eq(
            processed,
            rate,
            float(config.highpass_hz),
            float(config.lowpass_hz),
        )

    noise_type = config.noise_type.strip().lower()
    noise_db = None
    if noise_type != "none":
        processed = _mix_noise(processed, noise_type, float(config.noise_db), rng)
        noise_db = float(config.noise_db)

    processed = _finish(processed, rate)
    return AugmentResult(
        audio=processed,
        sample_rate=rate,
        pitch_semitones=float(pitch),
        speed=float(speed),
        highpass_hz=highpass,
        lowpass_hz=lowpass,
        eq_clamped=clamped,
        noise_type=noise_type,
        noise_db=noise_db,
        seed=seed,
        duration_in=float(duration_in),
        duration_out=processed.shape[-1] / rate,
        channels=int(processed.shape[0]),
    )


def _prepare_audio(audio: np.ndarray) -> np.ndarray:
    array = np.asarray(audio)
    if array.ndim == 2 and array.shape[0] > MAX_CHANNELS and array.shape[1] <= MAX_CHANNELS:
        array = array.T
    array = _as_channels(array)
    return np.array(array, dtype=np.float32, copy=True)


def _as_channels(audio: np.ndarray) -> np.ndarray:
    if audio.ndim == 1:
        return audio[np.newaxis, :]
    if audio.ndim != 2:
        raise AudioError("Dữ liệu âm thanh phải là mono hoặc đa kênh.")
    return audio


def _validate_audio(audio: np.ndarray, sample_rate: int) -> None:
    if sample_rate < MIN_SAMPLE_RATE:
        raise AudioError(
            f"Tần số lấy mẫu {sample_rate} Hz quá thấp. Cần từ {MIN_SAMPLE_RATE} Hz trở lên."
        )
    if audio.shape[0] > MAX_CHANNELS:
        raise AudioError(f"Chỉ hỗ trợ tối đa {MAX_CHANNELS} kênh.")
    duration = audio.shape[-1] / sample_rate
    if duration < MIN_DURATION_SEC:
        raise AudioError("File quá ngắn (dưới 0,3 giây).")
    if duration > MAX_DURATION_SEC:
        raise AudioError("File dài hơn 10 phút. Hãy cắt ngắn trước khi augmentation.")


def _validate_config(config: AugmentConfig) -> None:
    noise_type = config.noise_type.strip().lower()
    if noise_type not in {"pink", "white", "none"}:
        raise AudioError("Loại nhiễu phải là pink, white hoặc none.")
    config.noise_type = noise_type
    if config.seed is not None:
        seed = int(config.seed)
        if seed < 0 or seed > 2**31 - 1:
            raise AudioError("Seed phải là số nguyên từ 0 đến 2147483647.")
        config.seed = seed
    if not config.randomize:
        _require_range("Cao độ (semitone)", float(config.pitch_semitones), PITCH_MIN, PITCH_MAX)
        _require_range("Tốc độ", float(config.speed), SPEED_MIN, SPEED_MAX)
    if config.eq:
        _require_range("High-pass (Hz)", float(config.highpass_hz), HIGHPASS_MIN, HIGHPASS_MAX)
        _require_range("Low-pass (Hz)", float(config.lowpass_hz), LOWPASS_MIN, LOWPASS_MAX)
        if float(config.highpass_hz) >= float(config.lowpass_hz):
            raise AudioError("Tần số high-pass phải thấp hơn low-pass.")
    if noise_type != "none":
        _require_range("Mức nhiễu (dB)", float(config.noise_db), NOISE_DB_MIN, NOISE_DB_MAX)


def _require_range(name: str, value: float, low: float, high: float) -> None:
    if not math.isfinite(value) or value < low or value > high:
        raise AudioError(f"{name} phải nằm trong khoảng {low:g} đến {high:g}. Nhận được: {value:g}.")


def _make_rng(config: AugmentConfig) -> tuple[np.random.Generator, int | None]:
    seed = config.seed
    if seed is None and config.randomize:
        seed = random.SystemRandom().randrange(0, 2**31 - 1)
    return np.random.default_rng(None if seed is None else int(seed)), seed


def _resolve_motion(config: AugmentConfig, rng: np.random.Generator) -> tuple[float, float]:
    if not config.randomize:
        return float(config.pitch_semitones), float(config.speed)
    pitch = round(float(rng.uniform(PITCH_MIN, PITCH_MAX)), 2)
    speed = round(float(rng.uniform(SPEED_MIN, SPEED_MAX)), 3)
    pitch = min(PITCH_MAX, max(PITCH_MIN, pitch))
    speed = min(SPEED_MAX, max(SPEED_MIN, speed))
    return pitch, speed


def _pitch_shift(audio: np.ndarray, sample_rate: int, semitones: float) -> np.ndarray:
    import librosa

    channels = []
    for channel in audio:
        try:
            shifted = librosa.effects.pitch_shift(
                np.ascontiguousarray(channel, dtype=np.float32),
                sr=sample_rate,
                n_steps=float(semitones),
            )
        except Exception as exc:
            raise AudioError("Không đổi được cao độ. File có thể quá ngắn.") from exc
        channels.append(np.asarray(shifted, dtype=np.float32).reshape(-1))
    return _stack_channels(channels)


def _time_stretch(audio: np.ndarray, speed: float) -> np.ndarray:
    import librosa

    channels = []
    for channel in audio:
        try:
            stretched = librosa.effects.time_stretch(
                np.ascontiguousarray(channel, dtype=np.float32),
                rate=float(speed),
            )
        except Exception as exc:
            raise AudioError("Không đổi được tempo. File có thể quá ngắn.") from exc
        channels.append(np.asarray(stretched, dtype=np.float32).reshape(-1))
    return _stack_channels(channels)


def _apply_eq(
    audio: np.ndarray,
    sample_rate: int,
    highpass_hz: float,
    lowpass_hz: float,
) -> tuple[np.ndarray, float, float, bool]:
    nyquist = sample_rate / 2.0
    highpass = min(float(highpass_hz), nyquist * 0.45)
    lowpass = min(float(lowpass_hz), nyquist * 0.98)
    if highpass >= lowpass:
        raise AudioError(
            f"Sample rate {sample_rate} Hz không đủ để lọc "
            f"{highpass_hz:.0f}–{lowpass_hz:.0f} Hz."
        )
    clamped = abs(highpass - highpass_hz) > 0.5 or abs(lowpass - lowpass_hz) > 0.5
    try:
        sos_high = butter(4, highpass, btype="highpass", fs=sample_rate, output="sos")
        sos_low = butter(4, lowpass, btype="lowpass", fs=sample_rate, output="sos")
    except ValueError as exc:
        raise AudioError("Không thiết kế được bộ lọc với tần số này.") from exc

    channels = [_filter_channel(channel, sos_high, sos_low) for channel in audio]
    return _stack_channels(channels), float(highpass), float(lowpass), clamped


def _filter_channel(channel: np.ndarray, sos_high, sos_low) -> np.ndarray:
    filtered = _sos(sos_high, np.ascontiguousarray(channel, dtype=np.float64))
    return _sos(sos_low, filtered)


def _sos(sos, samples: np.ndarray) -> np.ndarray:
    try:
        return sosfiltfilt(sos, samples)
    except ValueError:
        return sosfilt(sos, samples)


def _mix_noise(
    audio: np.ndarray,
    noise_type: str,
    noise_db: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Trộn nhiễu sao cho RMS nhiễu = RMS tín hiệu + noise_db.

    −48 dB nghĩa là nhiễu nhỏ hơn tín hiệu khoảng 251 lần về biên độ.
    """
    gain = 10 ** (float(noise_db) / 20.0)
    mixed = []
    for channel in audio:
        count = channel.shape[0]
        if noise_type == "white":
            noise = rng.standard_normal(count)
        elif noise_type == "pink":
            noise = _pink_noise(count, rng)
        else:
            raise AudioError("Loại nhiễu phải là pink, white hoặc none.")
        noise = np.asarray(noise, dtype=np.float64)
        noise_rms = float(np.sqrt(np.mean(noise * noise))) + 1e-12
        noise /= noise_rms
        signal_rms = float(np.sqrt(np.mean(np.square(channel.astype(np.float64)))))
        reference = signal_rms if signal_rms > 1e-4 else 1.0
        mixed.append((channel.astype(np.float64) + noise * reference * gain).astype(np.float32))
    return np.stack(mixed)


def _pink_noise(count: int, rng: np.random.Generator) -> np.ndarray:
    """Nhiễu hồng 1/f bằng cách chia phổ của nhiễu trắng cho căn bậc hai tần số."""
    white = rng.standard_normal(count)
    spectrum = np.fft.rfft(white)
    frequencies = np.fft.rfftfreq(count, d=1.0)
    if frequencies.size > 1:
        frequencies[0] = frequencies[1]
    else:
        frequencies[0] = 1.0
    spectrum = spectrum / np.sqrt(frequencies)
    return np.fft.irfft(spectrum, n=count)


def _finish(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    finished = np.nan_to_num(audio.astype(np.float32, copy=False), nan=0.0, posinf=0.0, neginf=0.0)
    finished = _edge_fade(finished, sample_rate)
    peak = float(np.max(np.abs(finished))) if finished.size else 0.0
    if peak > 0.999:
        finished = finished / np.float32(peak) * np.float32(0.98)
    return np.ascontiguousarray(np.clip(finished, -1.0, 1.0), dtype=np.float32)


def _edge_fade(audio: np.ndarray, sample_rate: int, milliseconds: float = 5.0) -> np.ndarray:
    """Làm mượt 5 ms hai đầu để bộ lọc không tạo tiếng click."""
    faded = audio.copy()
    fade_count = min(int(sample_rate * milliseconds / 1000.0), faded.shape[-1] // 8)
    if fade_count < 2:
        return faded
    ramp = np.linspace(0.0, 1.0, fade_count, dtype=np.float32)
    faded[:, :fade_count] *= ramp
    faded[:, -fade_count:] *= ramp[::-1]
    return faded


def _stack_channels(channels: list[np.ndarray]) -> np.ndarray:
    width = min(len(channel) for channel in channels)
    if width <= 0:
        raise AudioError("Không còn mẫu âm thanh sau khi xử lý.")
    return np.stack([channel[:width].astype(np.float32, copy=False) for channel in channels])


def _interleaved(audio: np.ndarray) -> np.ndarray:
    if audio.shape[0] == 1:
        return np.ascontiguousarray(audio[0])
    return np.ascontiguousarray(audio.T)


def _wav_bytes_to_mp3(wav_bytes: bytes) -> bytes:
    ensure_output_format("mp3")
    try:
        from pydub import AudioSegment
    except ImportError as exc:
        raise AudioError(
            "Chưa cài pydub. Chạy: python -m pip install -r requirements.txt"
        ) from exc
    try:
        segment = AudioSegment.from_wav(io.BytesIO(wav_bytes))
        output = io.BytesIO()
        segment.export(output, format="mp3", bitrate="192k")
    except Exception as exc:
        raise AudioError(
            "Không xuất được MP3. Kiểm tra FFmpeg đã có trong PATH của terminal này chưa."
        ) from exc
    return output.getvalue()
