# -*- coding: utf-8 -*-
r"""Công cụ dòng lệnh Audio Augment.

Cài đặt:
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    winget install Gyan.FFmpeg

Ví dụ:
    python cli.py input.wav -o output.wav
    python cli.py input.wav -o output.mp3 --pitch 0.5 --speed 0.98
    python cli.py --demo -o demo_aug.wav
    python cli.py thu_muc_goc -o thu_muc_aug --variants 5
    python cli.py --help

Mặc định giữ tiếng tự nhiên: không đổi cao độ, không đổi tempo,
high-pass 70 Hz, low-pass 15 kHz, nhiễu hồng −48 dB.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processor import (
    AUDIO_EXTENSIONS,
    AudioError,
    AugmentConfig,
    augment,
    augment_file,
    ensure_output_format,
    format_summary,
    make_demo_tone,
    write_audio,
)
from youtube_clip import download_section


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except Exception:
            pass


class _HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio-augment",
        description=(
            "Biến đổi nhẹ file âm thanh để huấn luyện mô hình và thử DSP. "
            "Thứ tự: pitch, tempo, high-pass, low-pass, nhiễu nền."
        ),
        epilog=(
            "ví dụ:\n"
            "  python cli.py input.wav -o output.wav\n"
            "  python cli.py input.wav -o output.mp3 --pitch 0.5 --speed 0.98\n"
            "  python cli.py --demo -o demo_aug.wav\n"
            "  python cli.py thu_muc_goc -o thu_muc_aug --variants 5\n"
            "  python cli.py --youtube \"https://www.youtube.com/watch?v=...\" --section 1:12:00-1:13:00 -o D:\\ytmusic --keep 30\n"
        ),
        formatter_class=_HelpFormatter,
    )
    parser.add_argument("input", nargs="?", help="File hoặc thư mục âm thanh đầu vào")
    parser.add_argument("-o", "--output", help="File .wav/.mp3 hoặc thư mục đầu ra")
    parser.add_argument("--demo", action="store_true", help="Tạo đoạn âm mẫu rồi xử lý, không cần file vào")
    parser.add_argument("--pitch", type=float, default=0.0, help="Semitone, từ -1.5 đến 1.5")
    parser.add_argument("--speed", type=float, default=1.0, help="Tốc độ phát, từ 0.95 đến 1.05")
    parser.add_argument("--highpass", type=float, default=70.0, help="Tần số cắt high-pass, Hz")
    parser.add_argument("--lowpass", type=float, default=15000.0, help="Tần số cắt low-pass, Hz")
    parser.add_argument("--no-eq", action="store_true", help="Bỏ high-pass và low-pass")
    parser.add_argument("--noise", choices=["pink", "white", "none"], default="pink")
    parser.add_argument("--noise-db", type=float, default=-48.0, help="Gain nhiễu so với RMS tín hiệu")
    parser.add_argument("--seed", type=int, default=None, help="Seed để lặp lại nhiễu và chế độ ngẫu nhiên")
    parser.add_argument(
        "--randomize",
        action="store_true",
        help="Chọn ngẫu nhiên cao độ và tempo trong ngưỡng cho phép",
    )
    parser.add_argument("--variants", type=int, default=1, help="Số biến thể khi muốn đa dạng dữ liệu")
    parser.add_argument("--format", choices=["wav", "mp3"], default=None, help="Đuôi xuất. Mặc định theo -o, hoặc wav")
    parser.add_argument("--youtube", help="Link YouTube cần lấy audio")
    parser.add_argument("--section", help="Khoảng trên video, ví dụ 1:12:00-1:13:00")
    parser.add_argument("--keep", type=float, default=None, help="Số giây muốn giữ trong đoạn đã tải, ví dụ 30")
    parser.add_argument("--offset", type=float, default=0.0, help="Bắt đầu phần giữ từ giây này trong đoạn")
    parser.add_argument("--cookies", default=None, help="File cookies.txt. Mặc định dùng Downloads\\cookies.txt nếu có")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    if sys.version_info < (3, 10):
        print("Cần Python 3.10 trở lên.", file=sys.stderr)
        return 1

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.variants < 1 or args.variants > 30:
        parser.error("--variants phải từ 1 đến 30.")
    if args.youtube:
        try:
            return _run_youtube(args)
        except AudioError as exc:
            print(f"Lỗi: {exc}", file=sys.stderr)
            return 1
    if args.demo and args.input:
        parser.error("Chỉ dùng một trong hai: file đầu vào hoặc --demo.")
    if not args.demo and not args.input:
        parser.error("Hãy đưa file/thư mục đầu vào, hoặc thêm --demo.")

    randomize = bool(args.randomize or args.variants > 1)
    if args.variants > 1:
        print("Nhiều biến thể: cao độ và tempo được chọn ngẫu nhiên trong ngưỡng an toàn.")
    elif randomize:
        print("Cao độ và tempo ngẫu nhiên. --pitch và --speed được bỏ qua.")

    try:
        if args.demo:
            return _run_demo(args, randomize)
        source = Path(args.input)
        if source.is_dir():
            return _run_directory(source, args, randomize)
        if source.is_file():
            return _run_file(source, args, randomize)
    except AudioError as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        return 1

    print(f"Lỗi: không thấy {args.input}", file=sys.stderr)
    return 1


def _run_youtube(args: argparse.Namespace) -> int:
    if not args.section or "-" not in args.section:
        raise AudioError("Hãy đưa --section dạng 1:12:00-1:13:00.")
    start, end = args.section.split("-", 1)
    if not args.output:
        raise AudioError("Hãy chỉ -o là thư mục lưu, ví dụ D:\\ytmusic.")
    result = download_section(
        url=args.youtube,
        start=start.strip(),
        end=end.strip(),
        output_dir=args.output,
        keep_seconds=args.keep,
        offset_seconds=args.offset,
        cookies=args.cookies,
    )
    print(f"Đã lưu: {result.path}")
    print(
        f"{result.section_start} -> {result.section_end} | "
        f"{result.duration_sec:.2f}s | {result.title}"
    )
    return 0


def _config(args: argparse.Namespace, randomize: bool, seed: int | None) -> AugmentConfig:
    return AugmentConfig(
        pitch_semitones=args.pitch,
        speed=args.speed,
        highpass_hz=args.highpass,
        lowpass_hz=args.lowpass,
        eq=not args.no_eq,
        noise_type=args.noise,
        noise_db=args.noise_db,
        seed=seed,
        randomize=randomize,
    )


def _run_demo(args: argparse.Namespace, randomize: bool) -> int:
    output = Path(args.output) if args.output else Path("demo_aug.wav")
    fmt = ensure_output_format(args.format or _format_from_path(output))
    if _output_is_directory(output, args.variants):
        output.mkdir(parents=True, exist_ok=True)
        paths = [output / f"demo_aug_{index:02d}.{fmt}" for index in range(1, args.variants + 1)]
    else:
        paths = [output]
    base_seed = _base_seed(args.seed, randomize, len(paths))
    for index, path in enumerate(paths):
        audio, sample_rate = make_demo_tone()
        result = augment(audio, sample_rate, _config(args, randomize, _variant_seed(base_seed, index, randomize)))
        written = write_audio(str(path), result.audio, result.sample_rate, fmt)
        _print_done(written, result)
    return 0


def _run_file(source: Path, args: argparse.Namespace, randomize: bool) -> int:
    output = Path(args.output) if args.output else source.with_name(f"{source.stem}_aug.wav")
    fmt = ensure_output_format(args.format or _format_from_path(output))
    if _output_is_directory(output, args.variants):
        output.mkdir(parents=True, exist_ok=True)
        if args.variants == 1:
            paths = [output / f"{source.stem}_aug.{fmt}"]
        else:
            paths = [
                output / f"{source.stem}_aug_{index:02d}.{fmt}"
                for index in range(1, args.variants + 1)
            ]
    else:
        if output.resolve() == source.resolve():
            raise AudioError("File đầu ra trùng file đầu vào.")
        paths = [output.with_suffix(f".{fmt}")]
    return _process_paths(source, paths, args, randomize, fmt)


def _run_directory(source: Path, args: argparse.Namespace, randomize: bool) -> int:
    if not args.output:
        raise AudioError("Khi đầu vào là thư mục, hãy chỉ -o là thư mục đầu ra.")
    output = Path(args.output)
    fmt = ensure_output_format(args.format or "wav")
    files = _audio_files(source, output)
    if not files:
        raise AudioError(f"Không có file âm thanh trong {source}")
    output.mkdir(parents=True, exist_ok=True)
    ok = 0
    failed = 0
    for file_path in files:
        relative = file_path.relative_to(source)
        destination_dir = output / relative.parent
        if args.variants == 1:
            paths = [destination_dir / f"{file_path.stem}_aug.{fmt}"]
        else:
            paths = [
                destination_dir / f"{file_path.stem}_aug_{index:02d}.{fmt}"
                for index in range(1, args.variants + 1)
            ]
        try:
            _process_paths(file_path, paths, args, randomize, fmt)
            ok += 1
        except AudioError as exc:
            failed += 1
            print(f"Lỗi {file_path}: {exc}", file=sys.stderr)
    print(f"Xong: {ok} file, lỗi {failed} file.")
    return 1 if failed else 0


def _process_paths(
    source: Path,
    paths: list[Path],
    args: argparse.Namespace,
    randomize: bool,
    fmt: str,
) -> int:
    base_seed = _base_seed(args.seed, randomize, len(paths))
    for index, path in enumerate(paths):
        result = augment_file(str(source), _config(args, randomize, _variant_seed(base_seed, index, randomize)))
        written = write_audio(str(path), result.audio, result.sample_rate, fmt)
        _print_done(written, result)
    return 0


def _print_done(path: str, result) -> None:
    print(f"Đã xuất: {path}")
    print(format_summary(result))


def _audio_files(source: Path, output: Path) -> list[Path]:
    output_resolved = output.resolve()
    files = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        relative_parts = path.relative_to(source).parts
        if any(part.startswith(".") for part in relative_parts):
            continue
        try:
            path.resolve().relative_to(output_resolved)
            continue
        except ValueError:
            files.append(path)
    return files


def _output_is_directory(path: Path, variants: int) -> bool:
    if variants > 1:
        return True
    if path.exists() and path.is_dir():
        return True
    return path.suffix.lower() not in {".wav", ".mp3"}


def _format_from_path(path: Path) -> str:
    if path.suffix.lower() == ".mp3":
        return "mp3"
    return "wav"


def _base_seed(seed: int | None, randomize: bool, count: int) -> int | None:
    if seed is not None:
        return seed
    if randomize or count > 1:
        import random

        return random.SystemRandom().randrange(0, 2**31 - 1)
    return None


def _variant_seed(base_seed: int | None, index: int, randomize: bool) -> int | None:
    if base_seed is None:
        return None
    if randomize:
        return base_seed + index
    return base_seed


if __name__ == "__main__":
    raise SystemExit(main())
