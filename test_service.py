# -*- coding: utf-8 -*-
"""Kiểm tra contract microservice /v1. Chạy: python test_service.py"""

from __future__ import annotations

import base64
import io
import sys

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from api import app


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise SystemExit(f"FAIL {name} {detail}")
    print(f"OK {name}")


def wav_bytes(seconds: float = 0.5) -> bytes:
    sample_rate = 16000
    time = np.arange(int(sample_rate * seconds), dtype=np.float64) / sample_rate
    tone = (0.2 * np.sin(2 * np.pi * 440 * time)).astype(np.float32)
    buffer = io.BytesIO()
    sf.write(buffer, tone, sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def main() -> int:
    client = TestClient(app)

    health = client.get("/health")
    body = health.json()
    check("health", health.status_code == 200 and body["service"] == "audio" and body["status"] == "ok")

    catalog = client.get("/v1").json()
    paths = [item["path"] for item in catalog["operations"]]
    check("catalog lists operations", {"/v1/augment", "/v1/clip", "/v1/youtube"} <= set(paths), str(paths))

    rejected = client.post(
        "/v1/augment",
        files={"file": ("tone.wav", wav_bytes(), "audio/wav")},
        data={"pitch": "3", "noise_type": "none", "eq": "false"},
    )
    check("reject pitch out of range", rejected.status_code == 400, rejected.text)

    augmented = client.post(
        "/v1/augment",
        params={"response": "json"},
        files={"file": ("tone.wav", wav_bytes(), "audio/wav")},
        data={"pitch": "0", "speed": "1", "noise_type": "none", "eq": "false", "output_format": "wav"},
    )
    payload = augmented.json()
    check(
        "json augment",
        augmented.status_code == 200 and payload["service"] == "audio" and payload["meta"]["sample_rate"] == 16000,
        augmented.text[:300],
    )
    audio = base64.b64decode(payload["audio_base64"])
    parsed, rate = sf.read(io.BytesIO(audio))
    check("json audio decodes", rate == 16000 and len(parsed) > 1000)

    clipped = client.post(
        "/v1/clip",
        files={"file": ("tone.wav", wav_bytes(1.0), "audio/wav")},
        data={"seconds": "0.25"},
    )
    check("clip returns wav", clipped.status_code == 200 and clipped.headers["content-type"].startswith("audio/wav"), clipped.text[:200])
    clip_audio, clip_rate = sf.read(io.BytesIO(clipped.content))
    check("clip is about a quarter second", abs(len(clip_audio) / clip_rate - 0.25) < 0.05, f"{len(clip_audio) / clip_rate:.3f}")

    youtube = client.post("/v1/youtube", json={"url": "https://evil.example", "start": "0:01", "end": "0:02"})
    check("youtube rejects non-youtube url", youtube.status_code == 400, youtube.text)

    print("All service checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
