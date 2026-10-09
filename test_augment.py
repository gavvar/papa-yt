# -*- coding: utf-8 -*-
"""Kiểm tra pipeline augmentation. Chạy: python test_augment.py"""

from __future__ import annotations

import io
import sys

import numpy as np
import soundfile as sf

from processor import AudioError, AugmentConfig, audio_to_bytes, augment


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {name} {detail}")
    print(f"OK {name}")


def rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64))))) + 1e-12


def tone(frequency: float, sample_rate: int, seconds: float, amplitude: float = 0.4) -> np.ndarray:
    time = np.arange(int(sample_rate * seconds), dtype=np.float64) / sample_rate
    return (amplitude * np.sin(2 * np.pi * frequency * time)).astype(np.float32)


def bin_magnitude(samples: np.ndarray, sample_rate: int, frequency: float) -> float:
    window = samples * np.hanning(len(samples))
    spectrum = np.fft.rfft(window)
    index = int(round(frequency * len(samples) / sample_rate))
    return float(abs(spectrum[index]))


def test_ranges() -> None:
    sample_rate = 16000
    audio = tone(440, sample_rate, 0.5)[None, :]
    try:
        augment(audio, sample_rate, AugmentConfig(pitch_semitones=3, noise_type="none", eq=False))
    except AudioError:
        check("reject pitch out of range", True)
    else:
        check("reject pitch out of range", False)
    try:
        augment(audio, sample_rate, AugmentConfig(speed=1.2, noise_type="none", eq=False))
    except AudioError:
        check("reject speed out of range", True)
    else:
        check("reject speed out of range", False)


def test_filters() -> None:
    sample_rate = 16000
    low = tone(40, sample_rate, 2.0)
    mid = tone(1000, sample_rate, 2.0)
    high = tone(6000, sample_rate, 2.0)
    highpass_input = (low + mid).astype(np.float32)
    result = augment(
        highpass_input[None, :],
        sample_rate,
        AugmentConfig(noise_type="none", highpass_hz=70, lowpass_hz=7000),
    )
    before = bin_magnitude(highpass_input, sample_rate, 40) / bin_magnitude(highpass_input, sample_rate, 1000)
    after = bin_magnitude(result.audio[0], sample_rate, 40) / bin_magnitude(result.audio[0], sample_rate, 1000)
    check("highpass attenuates 40 Hz", after < before * 0.35, f"{before:.3f} -> {after:.3f}")

    lowpass_input = (mid + high).astype(np.float32)
    result = augment(
        lowpass_input[None, :],
        sample_rate,
        AugmentConfig(noise_type="none", highpass_hz=30, lowpass_hz=4000),
    )
    before = bin_magnitude(lowpass_input, sample_rate, 6000) / bin_magnitude(lowpass_input, sample_rate, 1000)
    after = bin_magnitude(result.audio[0], sample_rate, 6000) / bin_magnitude(result.audio[0], sample_rate, 1000)
    check("lowpass attenuates 6 kHz", after < before * 0.35, f"{before:.3f} -> {after:.3f}")


def test_noise_level_and_seed() -> None:
    sample_rate = 16000
    audio = tone(440, sample_rate, 1.0, amplitude=0.25)[None, :]
    clean = augment(audio, sample_rate, AugmentConfig(eq=False, noise_type="none", seed=3))
    noisy = augment(
        audio,
        sample_rate,
        AugmentConfig(eq=False, noise_type="pink", noise_db=-48, seed=3),
    )
    again = augment(
        audio,
        sample_rate,
        AugmentConfig(eq=False, noise_type="pink", noise_db=-48, seed=3),
    )
    ratio = 20 * np.log10(rms(noisy.audio - clean.audio) / rms(clean.audio))
    check("pink noise near -48 dB", abs(ratio + 48) < 1.5, f"{ratio:.2f} dB")
    check("same seed is repeatable", bool(np.allclose(noisy.audio, again.audio)))

    white = augment(
        audio,
        sample_rate,
        AugmentConfig(eq=False, noise_type="white", noise_db=-48, seed=9),
    )
    white_ratio = 20 * np.log10(rms(white.audio - clean.audio) / rms(clean.audio))
    check("white noise near -48 dB", abs(white_ratio + 48) < 1.5, f"{white_ratio:.2f} dB")


def test_motion_and_wav() -> None:
    sample_rate = 22050
    audio = tone(440, sample_rate, 1.0)[None, :]
    stretched = augment(audio, sample_rate, AugmentConfig(speed=1.05, eq=False, noise_type="none"))
    check(
        "faster tempo shortens audio",
        stretched.duration_out < stretched.duration_in * 0.98,
        f"{stretched.duration_in:.3f} -> {stretched.duration_out:.3f}",
    )
    shifted = augment(
        audio,
        sample_rate,
        AugmentConfig(pitch_semitones=1.5, eq=False, noise_type="none"),
    )
    check(
        "pitch shift keeps duration",
        abs(shifted.duration_out - shifted.duration_in) < 0.05,
        f"{shifted.duration_in:.3f} -> {shifted.duration_out:.3f}",
    )
    varied = augment(
        audio,
        sample_rate,
        AugmentConfig(eq=False, noise_type="none", randomize=True, seed=1),
    )
    check("random pitch stays inside range", -1.5 <= varied.pitch_semitones <= 1.5)
    check("random speed stays inside range", 0.95 <= varied.speed <= 1.05)

    payload = audio_to_bytes(shifted.audio, shifted.sample_rate, "wav")
    parsed, parsed_rate = sf.read(io.BytesIO(payload))
    check("wav bytes roundtrip", parsed_rate == sample_rate and parsed.shape[0] > 1000)


def test_m4a_uses_installed_ffmpeg() -> None:
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from processor import ffmpeg_available, load_audio

    if not ffmpeg_available():
        print("SKIP m4a, ffmpeg not on PATH")
        return
    ffmpeg = shutil.which("ffmpeg")
    with tempfile.TemporaryDirectory() as directory:
        target = str(Path(directory) / "tone.m4a")
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
                "sine=frequency=440:duration=1",
                "-c:a",
                "aac",
                target,
            ],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        audio, sample_rate = load_audio(target)
    check("m4a opens through ffmpeg", audio.shape[-1] > 1000 and sample_rate >= 8000, f"{audio.shape} {sample_rate}")


def main() -> int:
    test_m4a_uses_installed_ffmpeg()
    test_ranges()
    test_filters()
    test_noise_level_and_seed()
    test_motion_and_wav()
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
