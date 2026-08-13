#!/usr/bin/env python3
"""pdf2video — turn a PDF slideshow into a video, optionally with background music.

Each page of the PDF becomes a slide in the video. Slides are rendered at the
target resolution (letterboxed to preserve aspect ratio), shown for a fixed or
per-slide duration, optionally joined with crossfade transitions, and one or
more music tracks can be layered on top (played in order, looped or trimmed to
fit, with a fade-out at the end).

Requires: ffmpeg on PATH, and the PyMuPDF package (pip install pymupdf).

Examples:
    python pdf2video.py deck.pdf
    python pdf2video.py deck.pdf -o out.mp4 --duration 4 --music track.mp3
    python pdf2video.py deck.pdf --durations 5,3,3,8 --transition 0.75
    python pdf2video.py deck.pdf -m intro.mp3 -m main.mp3 --music-volume 0.5
"""

from __future__ import annotations

import argparse
import re
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

# Decks larger than this are encoded in parts and stitched together: one giant
# crossfade chain gets slow and can exceed the Windows command-length limit.
SINGLE_PASS_MAX = 30
CHUNK_SIZE = 20

DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", re.IGNORECASE)


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


def audio_duration(ffmpeg: str, track: Path) -> float:
    """Duration of an audio file in seconds, parsed from ffmpeg itself.

    Deliberately avoids ffprobe: some installs (e.g. the imageio-ffmpeg
    fallback) ship only the ffmpeg binary.
    """
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(track)], capture_output=True, text=True
    )
    match = DURATION_RE.search(result.stderr or "")
    if not match:
        sys.exit(f"error: could not read duration of {track} — is it a valid audio file?")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


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
        "-m", "--music", type=Path, action="append", default=None, metavar="FILE",
        help="audio file to play over the slideshow (mp3, wav, m4a, ...); "
             "repeat the flag to queue several tracks that play in order",
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
    for track in args.music or []:
        if not track.is_file():
            parser.error(f"music file not found: {track}")
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


def compute_fade(args: argparse.Namespace, durations: list[float]) -> float:
    fade = args.transition if len(durations) > 1 else 0.0
    # A crossfade needs both neighbors on screen; cap it so it fits in the
    # shortest slide.
    if fade > 0:
        fade = min(fade, min(durations) * 0.9)
    return fade


def scale_filter(args: argparse.Namespace) -> str:
    width, height = args.resolution
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={args.background},"
        f"setsar=1,format=yuv420p"
    )


def base_cmd(ffmpeg: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-stats", "-y"]


def video_codec_args(args: argparse.Namespace) -> list[str]:
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-r", str(args.fps)]


def build_audio(
    ffmpeg: str,
    args: argparse.Namespace,
    total: float,
    input_index: int,
) -> tuple[list[str], list[str], str | None]:
    """Music playlist inputs and filters.

    Tracks play in order; if looping, the whole playlist repeats to cover the
    video, then is trimmed. Returns (input_args, filter_strings, output_label);
    input_index is the ffmpeg input number of the first audio file.
    """
    if not args.music:
        return [], [], None
    playlist = list(args.music)
    if not args.no_loop_music:
        playlist_duration = sum(audio_duration(ffmpeg, t) for t in args.music)
        covered = playlist_duration
        while 0 < covered < total and len(playlist) < 500:
            playlist += args.music
            covered += playlist_duration

    inputs: list[str] = []
    for track in playlist:
        inputs += ["-i", str(track)]

    # Normalize every track to the same format so they can be joined,
    # regardless of each file's codec/sample rate/channel count.
    filters: list[str] = []
    normalize = "aformat=channel_layouts=stereo,aresample=44100"
    for j in range(len(playlist)):
        filters.append(f"[{input_index + j}:a]{normalize}[a{j}]")
    if len(playlist) > 1:
        chain = "".join(f"[a{j}]" for j in range(len(playlist)))
        filters.append(f"{chain}concat=n={len(playlist)}:v=0:a=1[acat]")
        joined = "acat"
    else:
        joined = "a0"

    audio_filters = [f"atrim=0:{total:.3f}", "asetpts=PTS-STARTPTS"]
    if args.music_volume != 1.0:
        audio_filters.append(f"volume={args.music_volume:.3f}")
    if args.music_fade > 0 and total > args.music_fade:
        start = total - args.music_fade
        audio_filters.append(f"afade=t=out:st={start:.3f}:d={args.music_fade:.3f}")
    filters.append(f"[{joined}]{','.join(audio_filters)}[aout]")
    return inputs, filters, "aout"


def build_concat_cmd(
    ffmpeg: str,
    slides: list[Path],
    durations: list[float],
    args: argparse.Namespace,
    list_path: Path,
    total: float,
) -> list[str]:
    """Hard cuts: feed all slides through ffmpeg's concat demuxer in one input.

    Scales to any number of slides without long command lines.
    """
    with open(list_path, "w", encoding="utf-8") as fh:
        fh.write("ffconcat version 1.0\n")
        for slide, dur in zip(slides, durations):
            fh.write(f"file '{slide.as_posix()}'\nduration {dur:.3f}\n")
        # concat-demuxer quirk: repeat the last file so its duration is honored
        fh.write(f"file '{slides[-1].as_posix()}'\n")

    a_inputs, a_filters, a_label = build_audio(ffmpeg, args, total, 1)
    cmd = base_cmd(ffmpeg) + ["-f", "concat", "-safe", "0", "-i", str(list_path)]
    cmd += a_inputs
    filters = [f"[0:v]{scale_filter(args)}[vout]"] + a_filters
    cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
    if a_label:
        cmd += ["-map", f"[{a_label}]", "-c:a", "aac", "-b:a", "192k"]
    cmd += video_codec_args(args)
    cmd += ["-movflags", "+faststart", "-t", f"{total:.3f}", str(args.output)]
    return cmd


def build_xfade_cmd(
    ffmpeg: str,
    seg_slides: list[Path],
    local_durs: list[float],
    lookahead: Path | None,
    fade: float,
    args: argparse.Namespace,
    out_path: Path,
    with_audio: bool,
) -> tuple[list[str], float]:
    """One crossfade-chained encode over seg_slides.

    `local_durs[i]` is the time slide i owns within this segment (display plus
    its trailing crossfade). `lookahead` is the next segment's first slide, so
    the crossfade into it lands at this segment's end; that slide's remaining
    display time is then owned by the next segment. Returns (cmd, duration).
    """
    chain: list[Path] = seg_slides + ([lookahead] if lookahead else [])
    # Each slide owns local_durs[i] minus the fade-in overlap owned by its
    # predecessor; the lookahead slide owns nothing here. Either way:
    seg_total = sum(local_durs) - fade * (len(local_durs) - 1)
    cmd = base_cmd(ffmpeg)

    # One looping still-image input per slide, long enough to cover its slot
    # plus the crossfade overlap into the next one.
    for i, slide in enumerate(seg_slides):
        extra = fade if i < len(chain) - 1 else 0.0
        cmd += ["-loop", "1", "-framerate", str(args.fps),
                "-t", f"{local_durs[i] + extra:.3f}", "-i", str(slide)]
    if lookahead:
        cmd += ["-loop", "1", "-framerate", str(args.fps),
                "-t", f"{fade + 1:.3f}", "-i", str(lookahead)]

    a_inputs, a_filters, a_label = ([], [], None)
    if with_audio:
        a_inputs, a_filters, a_label = build_audio(ffmpeg, args, seg_total, len(chain))
    cmd += a_inputs

    filters = [f"[{i}:v]{scale_filter(args)}[v{i}]" for i in range(len(chain))]
    if len(chain) == 1:
        last_label = "v0"
    else:
        prev = "v0"
        offset = 0.0
        for i in range(1, len(chain)):
            offset += local_durs[i - 1] - fade
            filters.append(
                f"[{prev}][v{i}]xfade=transition=fade:duration={fade:.3f}:offset={offset:.3f}[x{i}]"
            )
            prev = f"x{i}"
        last_label = prev
    filters += a_filters

    cmd += ["-filter_complex", ";".join(filters), "-map", f"[{last_label}]"]
    if a_label:
        cmd += ["-map", f"[{a_label}]", "-c:a", "aac", "-b:a", "192k"]
    cmd += video_codec_args(args)
    if out_path.suffix == ".mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-t", f"{seg_total:.3f}", str(out_path)]
    return cmd, seg_total


def build_combine_cmd(
    ffmpeg: str,
    seg_files: list[Path],
    args: argparse.Namespace,
    list_path: Path,
    total: float,
) -> list[str]:
    """Stitch encoded segments together (stream copy) and add the music."""
    with open(list_path, "w", encoding="utf-8") as fh:
        fh.write("ffconcat version 1.0\n")
        for seg in seg_files:
            fh.write(f"file '{seg.as_posix()}'\n")

    a_inputs, a_filters, a_label = build_audio(ffmpeg, args, total, 1)
    cmd = base_cmd(ffmpeg) + ["-f", "concat", "-safe", "0", "-i", str(list_path)]
    cmd += a_inputs
    if a_filters:
        cmd += ["-filter_complex", ";".join(a_filters)]
    cmd += ["-map", "0:v", "-c:v", "copy"]
    if a_label:
        cmd += ["-map", f"[{a_label}]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", "-t", f"{total:.3f}", str(args.output)]
    return cmd


def run_ffmpeg(cmd: list[str]) -> None:
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(f"error: ffmpeg failed (exit code {result.returncode})")


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

    with tempfile.TemporaryDirectory(prefix="pdf2video_") as tmp_name:
        tmp = Path(tmp_name)
        slides = render_slides(args.pdf, pages, args.resolution, tmp, args.quiet)
        n = len(slides)
        fade = compute_fade(args, durations)
        total = sum(durations) - fade * (n - 1)

        if not args.quiet:
            music_note = (
                f" with music from {', '.join(t.name for t in args.music)}"
                if args.music else ""
            )
            print(f"Encoding {total:.1f}s video{music_note}...")

        if fade == 0:
            run_ffmpeg(build_concat_cmd(
                ffmpeg, slides, durations, args, tmp / "slides.ffconcat", total))
        elif n <= SINGLE_PASS_MAX:
            cmd, _ = build_xfade_cmd(
                ffmpeg, slides, durations, None, fade, args, args.output, with_audio=True)
            run_ffmpeg(cmd)
        else:
            # Encode in parts (crossfades into the next part included), then
            # stitch with stream copy and lay the music over the whole video.
            starts = list(range(0, n, CHUNK_SIZE))
            seg_files: list[Path] = []
            base_time = 0.0
            for k, a in enumerate(starts):
                b = min(a + CHUNK_SIZE, n) - 1
                local = [durations[a] - (fade if a > 0 else 0.0)]
                local += [durations[i] for i in range(a + 1, b + 1)]
                lookahead = slides[b + 1] if b + 1 < n else None
                if not args.quiet:
                    print(f"  part {k + 1}/{len(starts)} (starts at {base_time:.1f}s)")
                seg = tmp / f"seg_{k:03d}.ts"
                cmd, seg_total = build_xfade_cmd(
                    ffmpeg, slides[a:b + 1], local, lookahead, fade, args, seg,
                    with_audio=False)
                run_ffmpeg(cmd)
                seg_files.append(seg)
                base_time += seg_total
            if not args.quiet:
                print("Combining parts..." if not args.music
                      else "Combining parts and adding music...")
            run_ffmpeg(build_combine_cmd(
                ffmpeg, seg_files, args, tmp / "segments.ffconcat", total))

    print(f"Done: {args.output} ({total:.1f}s)")


if __name__ == "__main__":
    main()
