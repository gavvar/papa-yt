# -*- coding: utf-8 -*-
r"""API và máy chủ giao diện Audio Augment.

Cài đặt:
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    winget install Gyan.FFmpeg

Chạy:
    .venv\Scripts\python.exe api.py

    Giao diện:    http://127.0.0.1:8000
    Tài liệu API: http://127.0.0.1:8000/docs

Ví dụ gọi API:
    curl -F "file=@input.wav" -F "pitch=0.4" -F "speed=1.02" ^
         http://127.0.0.1:8000/api/augment -o output.wav
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processor import (
    AUDIO_EXTENSIONS,
    AudioError,
    AugmentConfig,
    __version__,
    augment,
    augment_file,
    audio_to_bytes,
    clip_audio_bytes,
    ensure_output_format,
    get_capabilities,
    make_demo_tone,
    result_metadata,
)
from youtube_clip import choose_output_dir, download_section, youtube_defaults

WEB_DIR = ROOT / "web"
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
CONTENT_TYPES = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/aiff": ".aiff",
    "audio/x-aiff": ".aiff",
}

app = FastAPI(
    title="Audio service",
    version=__version__,
    description=(
        "Microservice audio. Gateway và các tool khác gọi /v1.\n\n"
        "GET /health và GET /v1 để khám phá service.\n"
        "POST /v1/augment, /v1/clip, /v1/youtube.\n"
        "Thêm ?response=json nếu caller cần audio_base64 thay vì file nhị phân.\n\n"
        "Giao diện local vẫn dùng /api."
    ),
)

_cors = os.environ.get("CORS_ORIGINS", "*").strip()
_origins = ["*"] if _cors in {"", "*"} else [item.strip() for item in _cors.split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Audio-Meta", "X-Augment-Meta", "X-Service", "Content-Disposition"],
)

from service_api import router as service_router

app.include_router(service_router)


@app.get("/api/health", tags=["meta"], summary="Trạng thái máy chủ")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/api/capabilities", tags=["meta"], summary="Giới hạn tham số và khả năng của máy")
def capabilities() -> dict:
    payload = get_capabilities()
    payload["youtube"] = youtube_defaults()
    return payload


@app.post("/api/pick-folder", tags=["youtube"], summary="Mở hộp thoại chọn thư mục lưu")
def pick_folder(initial: str = Form("")) -> dict:
    try:
        return {"path": choose_output_dir(initial)}
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("folder picker failed")
        raise HTTPException(status_code=500, detail="Không mở được hộp chọn thư mục.") from exc


@app.post("/api/clip", tags=["augment"], summary="Cắt số giây đầu của file kết quả để tải về")
async def clip_result(
    file: UploadFile = File(...),
    seconds: float = Form(...),
) -> Response:
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="File rỗng.")
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    try:
        clipped, extension = clip_audio_bytes(payload, suffix, seconds)
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    filename = f"{Path(file.filename or 'audio').stem[:80]}_clip{extension}"
    ascii_name = filename.encode("ascii", "ignore").decode() or f"audio_clip{extension}"
    media_type = "audio/mpeg" if extension == ".mp3" else "audio/wav"
    return Response(
        content=clipped,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/youtube", tags=["youtube"], summary="Tải một khoảng YouTube và lưu MP3")
def download_youtube(
    url: str = Form(..., description="Link https://www.youtube.com/watch?v=..."),
    start: str = Form(..., description="Thời điểm bắt đầu, ví dụ 1:12:00"),
    end: str = Form(..., description="Thời điểm kết thúc, ví dụ 1:13:00"),
    output_dir: str = Form(..., description="Thư mục lưu file. Anh chọn mỗi lần tải."),
    cookies: str = Form("", description="File cookies.txt. Để trống sẽ dùng Downloads\\cookies.txt nếu có."),
) -> Response:
    try:
        result = download_section(
            url=url,
            start=start,
            end=end,
            output_dir=output_dir,
            cookies=cookies,
        )
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logging.exception("youtube download failed")
        raise HTTPException(status_code=500, detail="Không tải được đoạn YouTube.") from exc
    payload = result.path.read_bytes()
    meta = quote(json.dumps(result.metadata(), ensure_ascii=True, separators=(",", ":")))
    filename = result.path.name
    ascii_name = filename.encode("ascii", "ignore").decode() or "youtube.mp3"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    logging.info("saved youtube clip %s", result.path)
    return Response(
        content=payload,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": disposition,
            "X-Augment-Meta": meta,
            "X-Saved-Path": quote(str(result.path)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/augment", tags=["augment"], summary="Augment một file âm thanh")
async def augment_upload(
    file: UploadFile = File(..., description="WAV, MP3, FLAC, OGG, M4A hoặc AIFF"),
    pitch: float = Form(0.0, description="Semitone, từ -1.5 đến 1.5. Mặc định 0."),
    speed: float = Form(1.0, description="Tốc độ phát, từ 0.95 đến 1.05. Mặc định 1."),
    highpass_hz: float = Form(70.0, description="High-pass, Hz. Mặc định 70."),
    lowpass_hz: float = Form(15000.0, description="Low-pass, Hz. Mặc định 15000."),
    eq: str = Form("true", description="true để bật EQ, false để tắt."),
    noise_type: str = Form("pink", description="pink, white hoặc none."),
    noise_db: float = Form(-48.0, description="Gain nhiễu so với RMS tín hiệu. Mặc định -48."),
    output_format: str = Form("wav", description="wav hoặc mp3."),
    seed: str = Form("", description="Seed nguyên, để trống nếu không cần lặp lại."),
    randomize: str = Form("false", description="true để chọn ngẫu nhiên pitch và tempo trong ngưỡng."),
) -> Response:
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="File rỗng.")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File lớn hơn 40 MB.")
    suffix = _input_suffix(file.filename or "", file.content_type or "")
    config = _config_from_form(
        pitch, speed, highpass_hz, lowpass_hz, eq, noise_type, noise_db, seed, randomize
    )
    fmt = _guard_format(output_format)
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / f"input{suffix}"
        source.write_bytes(payload)
        result = _run(lambda: augment_file(str(source), config))
    filename = _download_name(file.filename or "audio", fmt)
    logging.info("augmented %s %s", file.filename, result_metadata(result))
    return _render(result, fmt, filename)


@app.post("/api/demo", tags=["augment"], summary="Xử lý một đoạn âm thanh mẫu do máy chủ tạo")
def augment_demo(
    pitch: float = Form(0.0),
    speed: float = Form(1.0),
    highpass_hz: float = Form(70.0),
    lowpass_hz: float = Form(15000.0),
    eq: str = Form("true"),
    noise_type: str = Form("pink"),
    noise_db: float = Form(-48.0),
    output_format: str = Form("wav"),
    seed: str = Form(""),
    randomize: str = Form("false"),
) -> Response:
    config = _config_from_form(
        pitch, speed, highpass_hz, lowpass_hz, eq, noise_type, noise_db, seed, randomize
    )
    fmt = _guard_format(output_format)
    audio, sample_rate = make_demo_tone()
    result = _run(lambda: augment(audio, sample_rate, config))
    return _render(result, fmt, f"am_mau_aug.{fmt}")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def _config_from_form(
    pitch: float,
    speed: float,
    highpass_hz: float,
    lowpass_hz: float,
    eq: str,
    noise_type: str,
    noise_db: float,
    seed: str,
    randomize: str,
) -> AugmentConfig:
    try:
        return AugmentConfig(
            pitch_semitones=pitch,
            speed=speed,
            highpass_hz=highpass_hz,
            lowpass_hz=lowpass_hz,
            eq=_parse_bool(eq, "EQ"),
            noise_type=noise_type,
            noise_db=noise_db,
            seed=_parse_seed(seed),
            randomize=_parse_bool(randomize, "Ngẫu nhiên"),
        )
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _run(action):
    try:
        return action()
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logging.exception("augment failed")
        raise HTTPException(
            status_code=500,
            detail="Lỗi xử lý âm thanh. Xem log máy chủ để biết chi tiết.",
        ) from exc


def _guard_format(output_format: str) -> str:
    try:
        return ensure_output_format(output_format)
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _parse_bool(raw: str, name: str) -> bool:
    value = (raw or "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise AudioError(f"{name} phải là true hoặc false.")


def _parse_seed(raw: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    if not re.fullmatch(r"\d+", text):
        raise AudioError("Seed phải là số nguyên không âm.")
    value = int(text)
    if value > 2**31 - 1:
        raise AudioError("Seed phải từ 0 đến 2147483647.")
    return value


def _input_suffix(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in AUDIO_EXTENSIONS:
        return suffix
    mapped = CONTENT_TYPES.get(content_type.split(";")[0].strip().lower())
    if mapped in AUDIO_EXTENSIONS:
        return mapped
    raise HTTPException(
        status_code=400,
        detail="Định dạng chưa hỗ trợ. Dùng WAV, MP3, FLAC, OGG, M4A hoặc AIFF.",
    )


def _download_name(original: str, fmt: str) -> str:
    stem = Path(original).stem
    stem = re.sub(r"[^\w\s.\-()\[\]]+", "_", stem, flags=re.UNICODE).strip(" .") or "audio"
    return f"{stem[:80]}_aug.{fmt}"


def _render(result, fmt: str, filename: str) -> Response:
    try:
        payload = audio_to_bytes(result.audio, result.sample_rate, fmt)
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _audio_response(payload, fmt, filename, result)


def _audio_response(payload: bytes, fmt: str, filename: str, result) -> Response:
    ascii_stem = Path(filename).stem.encode("ascii", "ignore").decode() or "audio"
    ascii_name = f"{ascii_stem}_aug.{fmt}"
    disposition = (
        f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    )
    meta = quote(json.dumps(result_metadata(result), ensure_ascii=True, separators=(",", ":")))
    media_type = "audio/mpeg" if fmt == "mp3" else "audio/wav"
    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "Content-Disposition": disposition,
            "X-Augment-Meta": meta,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _open_browser_later(url: str) -> None:
    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:
            logging.info("Không tự mở được trình duyệt. Hãy vào %s", url)

    threading.Timer(0.8, _open).start()


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Chạy API và giao diện Audio Augment")
    parser.add_argument("--host", default=os.environ.get("AUDIO_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", os.environ.get("AUDIO_PORT", "8000"))))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        print("Cần Python 3.10 trở lên.", file=sys.stderr)
        return 1

    import uvicorn

    display_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    url = f"http://{display_host}:{args.port}"
    print(f"Audio Augment đang chạy tại {url}")
    print(f"Tài liệu API: {url}/docs")
    if not args.no_browser:
        _open_browser_later(url)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    raise SystemExit(main())
