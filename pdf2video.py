#!/usr/bin/env python3
"""pdf2video — turn a PDF slideshow into a video, optionally with background music.

Each page of the PDF becomes a slide in the video. Slides are rendered at the
target resolution (letterboxed to preserve aspect ratio), shown for a fixed or
per-slide duration, optionally joined with crossfade transitions, and a music
track can be layered on top (looped or trimmed to fit, with a fade-out at the
end).

Requires: ffmpeg on PATH, and the PyMuPDF package (pip install pymupdf).

Examples:
    python pdf2video.py deck.pdf
    python pdf2video.py deck.pdf -o out.mp4 --duration 4 --music track.mp3
    python pdf2video.py deck.pdf --durations 5,3,3,8 --transition 0.75
    python pdf2video.py deck.pdf --music track.mp3 --music-volume 0.5 --no-loop-music
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz  # older PyMuPDF releases
    except ImportError:  # pragma: no cover
        sys.exit("error: PyMuPDF is required — install it with: pip install pymupdf")

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma"}


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit(
            "error: ffmpeg not found on PATH.\n"
            "Install it (https://ffmpeg.org/download.html) or run: pip install imageio-ffmpeg"
        )


def parse_resolution(value: str) -> tuple[int, int]:
    presets = {
        "1080p": (1920, 1080),
        "720p": (1280, 720),
        "4k": (3840, 2160),
        "vertical": (1080, 1920),
        "square": (1080, 1080),
    }
    if value.lower() in presets:
        return presets[value.lower()]
    try:
        w, h = value.lower().split("x")
        width, height = int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid resolution {value!r} — use WIDTHxHEIGHT (e.g. 1920x1080) "
            f"or a preset: {', '.join(presets)}"
        )
    if width < 16 or height < 16:
        raise argparse.ArgumentTypeError("resolution too small")
    # H.264 needs even dimensions.
    return width - width % 2, height - height % 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pdf2video",
        description="Convert a PDF slideshow into a video, optionally with background music.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("pdf", type=Path, help="input PDF file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="output video file (default: <pdf name>.mp4)",
    )
    parser.add_argument(
        "-d", "--duration", type=float, default=5.0,
        help="seconds each slide is shown",
    )
    parser.add_argument(
        "--durations", default=None,
        help="comma-separated per-slide durations in seconds, e.g. 5,3,3,8 "
             "(overrides --duration; if fewer values than slides, the last value repeats)",
    )
    parser.add_argument(
        "-m", "--music", type=Path, default=None,
        help="audio file to play over the slideshow (mp3, wav, m4a, ...)",
    )
    parser.add_argument(
        "--music-volume", type=float, default=1.0,
        help="music volume multiplier (0.0-2.0)",
    )
    parser.add_argument(
        "--no-loop-music", action="store_true",
        help="don't loop the music if it's shorter than the video",
    )
    parser.add_argument(
        "--music-fade", type=float, default=2.0,
        help="seconds of audio fade-out at the end of the video (0 to disable)",
    )
    parser.add_argument(
        "-t", "--transition", type=float, default=0.5, metavar="SECONDS",
        help="crossfade duration between slides (0 for hard cuts)",
    )
    parser.add_argument(
        "-r", "--resolution", type=parse_resolution, default="1080p",
        help="output size: WIDTHxHEIGHT or preset (1080p, 720p, 4k, vertical, square)",
    )
    parser.add_argument("--fps", type=int, default=30, help="output frames per second")
    parser.add_argument(
        "--background", default="black",
        help="letterbox color (ffmpeg color name or #RRGGBB)",
    )
    parser.add_argument(
        "--pages", default=None,
        help="pages to include, 1-based, e.g. 1-5,8,10 (default: all)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="less output")
    args = parser.parse_args(argv)

    if not args.pdf.is_file():
        parser.error(f"PDF not found: {args.pdf}")
    if args.pdf.suffix.lower() in AUDIO_EXTENSIONS:
        parser.error(f"{args.pdf} looks like an audio file — pass the PDF first, music via --music")
    if args.music is not None and not args.music.is_file():
        parser.error(f"music file not found: {args.music}")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if not 0 <= args.music_volume <= 2:
        parser.error("--music-volume must be between 0.0 and 2.0")
    if args.transition < 0:
        parser.error("--transition cannot be negative")
    if args.output is None:
        args.output = args.pdf.with_suffix(".mp4")
    return args


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    """Return 0-based page indices from a 1-based spec like '1-5,8,10'."""
    if spec is None:
        return list(range(page_count))
    indices: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            start, end = int(lo), int(hi)
        else:
            start = end = int(part)
        if start < 1 or end > page_count or start > end:
            sys.exit(f"error: page range {part!r} is out of bounds (PDF has {page_count} pages)")
        indices.extend(range(start - 1, end))
    if not indices:
        sys.exit("error: --pages selected no pages")
    return indices


def parse_durations(args: argparse.Namespace, slide_count: int) -> list[float]:
    if args.durations is None:
        return [args.duration] * slide_count
    try:
        values = [float(v) for v in args.durations.split(",") if v.strip()]
    except ValueError:
        sys.exit(f"error: could not parse --durations {args.durations!r}")
    if not values or any(v <= 0 for v in values):
        sys.exit("error: --durations values must be positive numbers")
    # Pad by repeating the last value; truncate extras.
    values = (values + [values[-1]] * slide_count)[:slide_count]
    return values


def render_slides(
    pdf_path: Path,
    pages: list[int],
    size: tuple[int, int],
    out_dir: Path,
    quiet: bool,
) -> list[Path]:
    """Render selected PDF pages to PNGs at the target size, letterboxed."""
    width, height = size
    slides: list[Path] = []
    with fitz.open(pdf_path) as doc:
        for i, page_index in enumerate(pages):
            page = doc[page_index]
            rect = page.rect
            zoom = min(width / rect.width, height / rect.height)
            # Render at 2x then let ffmpeg's scaler downsample for crisp text,
            # unless that would produce an enormous bitmap.
            supersample = 2 if max(width, height) <= 2048 else 1
            matrix = fitz.Matrix(zoom * supersample, zoom * supersample)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            out = out_dir / f"slide_{i:04d}.png"
            pixmap.save(out)
            slides.append(out)
            if not quiet:
                print(f"  rendered page {page_index + 1} -> {out.name}")
    return slides


def build_ffmpeg_command(
    ffmpeg: str,
    slides: list[Path],
    durations: list[float],
    args: argparse.Namespace,
) -> tuple[list[str], float]:
    """Assemble the ffmpeg invocation. Returns (command, video duration)."""
    width, height = args.resolution
    fade = args.transition if len(slides) > 1 else 0.0
    # A crossfade needs both neighbors on screen; cap it so it fits in the
    # shortest slide.
    if fade > 0:
        fade = min(fade, min(durations) * 0.9)

    total = sum(durations) - fade * (len(slides) - 1)

    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-stats", "-y"]

    # One looping still-image input per slide. Each input runs long enough to
    # cover its slide plus the crossfade overlap into the next one.
    for i, (slide, dur) in enumerate(zip(slides, durations)):
        input_duration = dur + (fade if i < len(slides) - 1 else 0)
        cmd += ["-loop", "1", "-framerate", str(args.fps), "-t", f"{input_duration:.3f}", "-i", str(slide)]

    music_index = None
    if args.music is not None:
        music_index = len(slides)
        if args.no_loop_music:
            cmd += ["-i", str(args.music)]
        else:
            cmd += ["-stream_loop", "-1", "-i", str(args.music)]

    # Video filter graph: scale/pad every slide, then chain xfades (or concat).
    filters = []
    scale = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={args.background},"
        f"setsar=1,format=yuv420p"
    )
    for i in range(len(slides)):
        filters.append(f"[{i}:v]{scale}[v{i}]")

    if len(slides) == 1:
        last_label = "v0"
    elif fade > 0:
        prev = "v0"
        offset = 0.0
        for i in range(1, len(slides)):
            offset += durations[i - 1] - fade
            label = f"x{i}"
            filters.append(
                f"[{prev}][v{i}]xfade=transition=fade:duration={fade:.3f}:offset={offset:.3f}[{label}]"
            )
            prev = label
        last_label = prev
    else:
        chain = "".join(f"[v{i}]" for i in range(len(slides)))
        filters.append(f"{chain}concat=n={len(slides)}:v=1:a=0[vout]")
        last_label = "vout"

    audio_label = None
    if music_index is not None:
        audio_filters = [f"atrim=0:{total:.3f}", "asetpts=PTS-STARTPTS"]
        if args.music_volume != 1.0:
            audio_filters.append(f"volume={args.music_volume:.3f}")
        if args.music_fade > 0 and total > args.music_fade:
            start = total - args.music_fade
            audio_filters.append(f"afade=t=out:st={start:.3f}:d={args.music_fade:.3f}")
        filters.append(f"[{music_index}:a]{','.join(audio_filters)}[aout]")
        audio_label = "aout"

    cmd += ["-filter_complex", ";".join(filters), "-map", f"[{last_label}]"]
    if audio_label:
        cmd += ["-map", f"[{audio_label}]", "-c:a", "aac", "-b:a", "192k"]
    cmd += [
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-r", str(args.fps),
        "-movflags", "+faststart",
        "-t", f"{total:.3f}",
        str(args.output),
    ]
    return cmd, total


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    ffmpeg = find_ffmpeg()

    with fitz.open(args.pdf) as doc:
        page_count = doc.page_count
    if page_count == 0:
        sys.exit(f"error: {args.pdf} has no pages")
    pages = parse_pages(args.pages, page_count)
    durations = parse_durations(args, len(pages))

    if not args.quiet:
        print(f"Rendering {len(pages)} slide(s) from {args.pdf.name} "
              f"at {args.resolution[0]}x{args.resolution[1]}...")

    with tempfile.TemporaryDirectory(prefix="pdf2video_") as tmp:
        slides = render_slides(args.pdf, pages, args.resolution, Path(tmp), args.quiet)
        cmd, total = build_ffmpeg_command(ffmpeg, slides, durations, args)
        if not args.quiet:
            music_note = f" with music from {args.music.name}" if args.music else ""
            print(f"Encoding {total:.1f}s video{music_note}...")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            sys.exit(f"error: ffmpeg failed (exit code {result.returncode})")

    print(f"Done: {args.output} ({total:.1f}s)")


if __name__ == "__main__":
    main()
