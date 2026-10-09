# -*- coding: utf-8 -*-
r"""API microservice cho tool audio.

Service name: audio
Các tool khác gọi prefix /v1. Giao diện local vẫn dùng /api.

Chạy cùng các service khác
--------------------------
    uvicorn api:app --host 0.0.0.0 --port 8000

Biến môi trường:
    PORT            cổng, mặc định 8000
    AUDIO_HOST      mặc định 127.0.0.1; trên server đặt 0.0.0.0
    CORS_ORIGINS    * hoặc danh sách cách nhau bởi dấu phẩy

Khám phá service:
    GET /health
    GET /v1

Augment, trả file WAV:
    curl -F "file=@input.wav" -F "pitch=0.4" -F "speed=1.02" ^
         http://HOST:8000/v1/augment -o out.wav

Augment, trả JSON để service khác đọc:
    curl -F "file=@input.wav" "http://HOST:8000/v1/augment?response=json"

Cắt số giây đầu của file kết quả:
    curl -F "file=@out.wav" -F "seconds=30" http://HOST:8000/v1/clip -o clip.wav

Tải một khoảng YouTube, không ghi vào đĩa của server:
    curl -H "Content-Type: application/json" ^
         -d "{\"url\":\"https://www.youtube.com/watch?v=...\",\"start\":\"1:12:00\",\"end\":\"1:13:00\"}" ^
         http://HOST:8000/v1/youtube -o clip.mp3

File trả về kèm header X-Audio-Meta (JSON đã encode). response=json thì body là
{"service","filename","media_type","meta","audio_base64"}.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

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
    ffmpeg_available,
    get_capabilities,
    make_demo_tone,
    result_metadata,
)
from youtube_clip import download_section, yt_dlp_available

SERVICE_NAME = "audio"
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

router = APIRouter()


class YoutubeRequest(BaseModel):
    url: str = Field(description="Link https://www.youtube.com/watch?v=...")
    start: str = Field(description="Thời điểm bắt đầu, ví dụ 1:12:00")
    end: str = Field(description="Thời điểm kết thúc, ví dụ 1:13:00")
    cookies: str | None = Field(default=None, description="Nội dung cookies.txt, không phải đường dẫn trên server")


@router.get("/health", tags=["service"], summary="Health check cho gateway và container")
@router.get("/v1/health", tags=["service"], summary="Health check phiên bản v1")
def health() -> dict:
    return {"status": "ok", "service": SERVICE_NAME, "version": __version__}


@router.get("/v1", tags=["service"], summary="Mô tả service để gắn vào gateway")
def describe() -> dict:
    caps = get_capabilities()
    return {
        "service": SERVICE_NAME,
        "version": __version__,
        "ffmpeg": ffmpeg_available(),
        "yt_dlp": yt_dlp_available(),
        "limits": {
            "max_upload_mb": caps["max_upload_mb"],
            "max_duration_sec": caps["max_duration_sec"],
            "input_extensions": caps["input_extensions"],
            "output_formats": caps["output_formats"],
        },
        "spec": caps["spec"],
        "operations": [
            {"method": "POST", "path": "/v1/augment", "input": "multipart file", "output": "audio hoặc JSON"},
            {"method": "POST", "path": "/v1/clip", "input": "multipart file + seconds", "output": "audio hoặc JSON"},
            {"method": "POST", "path": "/v1/youtube", "input": "JSON url, start, end", "output": "audio/mpeg hoặc JSON"},
            {"method": "POST", "path": "/v1/demo", "input": "form thông số", "output": "audio hoặc JSON"},
        ],
    }


@router.post("/v1/augment", tags=["service"], summary="Augment một file, không lưu trên server")
async def augment_service(
    file: UploadFile = File(...),
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
    response: str = Query("file", pattern="^(file|json)$"),
) -> Response:
    payload = await _read_upload(file)
    suffix = _input_suffix(file.filename or "", file.content_type or "")
    config = _config(pitch, speed, highpass_hz, lowpass_hz, eq, noise_type, noise_db, seed, randomize)
    fmt = _format(output_format)
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / f"input{suffix}"
        source.write_bytes(payload)
        result = _call(lambda: augment_file(str(source), config))
    filename = _download_name(file.filename or "audio", fmt)
    audio = _call(lambda: audio_to_bytes(result.audio, result.sample_rate, fmt))
    return _media(audio, _media_type(fmt), filename, result_metadata(result), response)


@router.post("/v1/clip", tags=["service"], summary="Cắt số giây đầu của file kết quả")
async def clip_service(
    file: UploadFile = File(...),
    seconds: float = Form(...),
    response: str = Query("file", pattern="^(file|json)$"),
) -> Response:
    payload = await _read_upload(file)
    suffix = Path(file.filename or "audio.wav").suffix or ".wav"
    clipped, extension = _call(lambda: clip_audio_bytes(payload, suffix, seconds))
    filename = f"{_stem(file.filename or 'audio')}_clip{extension}"
    return _media(
        clipped,
        _media_type(extension.lstrip(".")),
        filename,
        {"seconds": seconds, "filename": filename},
        response,
    )


@router.post("/v1/youtube", tags=["service"], summary="Tải một khoảng YouTube và trả MP3")
def youtube_service(
    body: YoutubeRequest,
    response: str = Query("file", pattern="^(file|json)$"),
) -> Response:
    with tempfile.TemporaryDirectory(prefix="audio-yt-") as directory:
        folder = Path(directory)
        cookie_path = None
        if body.cookies and body.cookies.strip():
            cookie_path = folder / "cookies.txt"
            cookie_path.write_text(body.cookies, encoding="utf-8")
        result = _call(
            lambda: download_section(
                url=body.url,
                start=body.start,
                end=body.end,
                output_dir=folder,
                cookies=cookie_path,
            )
        )
        payload = result.path.read_bytes()
        filename = result.path.name
        meta = result.metadata()
    meta["saved_path"] = None
    return _media(payload, "audio/mpeg", filename, meta, response)


@router.post("/v1/demo", tags=["service"], summary="Chạy pipeline trên âm mẫu, dùng để kiểm tra service")
def demo_service(
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
    response: str = Query("file", pattern="^(file|json)$"),
) -> Response:
    config = _config(pitch, speed, highpass_hz, lowpass_hz, eq, noise_type, noise_db, seed, randomize)
    fmt = _format(output_format)
    audio, sample_rate = make_demo_tone()
    result = _call(lambda: augment(audio, sample_rate, config))
    payload = _call(lambda: audio_to_bytes(result.audio, result.sample_rate, fmt))
    return _media(payload, _media_type(fmt), f"am_mau.{fmt}", result_metadata(result), response)


def _media(payload: bytes, media_type: str, filename: str, meta: dict, response_mode: str) -> Response:
    encoded_meta = quote(json.dumps(meta, ensure_ascii=True, separators=(",", ":")))
    if response_mode == "json":
        return JSONResponse(
            {
                "service": SERVICE_NAME,
                "ok": True,
                "filename": filename,
                "media_type": media_type,
                "meta": meta,
                "audio_base64": base64.b64encode(payload).decode("ascii"),
            },
            headers={"X-Service": SERVICE_NAME, "X-Audio-Meta": encoded_meta},
        )
    ascii_name = filename.encode("ascii", "ignore").decode() or "audio"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "Content-Disposition": disposition,
            "X-Service": SERVICE_NAME,
            "X-Audio-Meta": encoded_meta,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _call(action):
    try:
        return action()
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logging.exception("audio service failed")
        raise HTTPException(status_code=500, detail="Lỗi xử lý của service audio.") from exc


def _config(
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
            eq=_parse_bool(eq),
            noise_type=noise_type,
            noise_db=noise_db,
            seed=_parse_seed(seed),
            randomize=_parse_bool(randomize),
        )
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _format(output_format: str) -> str:
    try:
        return ensure_output_format(output_format)
    except AudioError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _parse_bool(raw: str) -> bool:
    value = (raw or "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise AudioError("Giá trị đúng/sai phải là true hoặc false.")


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


async def _read_upload(file: UploadFile) -> bytes:
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="File rỗng.")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File lớn hơn 40 MB.")
    return payload


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


def _stem(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"[^\w\s.\-()\[\]]+", "_", stem, flags=re.UNICODE).strip(" .") or "audio"
    return stem[:80]


def _download_name(original: str, fmt: str) -> str:
    return f"{_stem(original)}_aug.{fmt}"


def _media_type(fmt: str) -> str:
    return "audio/mpeg" if fmt == "mp3" else "audio/wav"
