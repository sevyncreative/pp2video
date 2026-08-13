# pp2video

Turn a PDF slideshow into a video — with optional background music.

Each page of the PDF becomes a slide. Slides are rendered at your target
resolution (letterboxed to preserve aspect ratio), shown for a fixed or
per-slide duration, joined with crossfade transitions (or hard cuts), and a
music track can be layered on top — looped or trimmed to fit the video, with a
fade-out at the end.

## Requirements

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/download.html) on your PATH
  (or `pip install imageio-ffmpeg` as a fallback)
- PyMuPDF: `pip install -r requirements.txt`

## GUI

```bash
python pdf2video_gui.py
```

Opens a window where you pick the PDF, optionally a music track, and the
output location, then adjust slide timing, crossfades, resolution, music
volume/looping/fade-out, and page selection. A progress bar tracks the encode,
and "Open folder" jumps to the finished video. The GUI needs only the Python
standard library (tkinter, bundled with Python on Windows/macOS; on Linux
install it with e.g. `sudo apt install python3-tk`).

## Command line

```bash
# Basic: 5 seconds per slide, output next to the PDF as deck.mp4
python pdf2video.py deck.pdf

# With music looped over the whole video
python pdf2video.py deck.pdf --music track.mp3

# 3s per slide, softer music, 1s crossfades, custom output path
python pdf2video.py deck.pdf -o final.mp4 -d 3 -m track.mp3 --music-volume 0.6 -t 1
```

## Options

| Option | Default | Description |
|---|---|---|
| `-o, --output` | `<pdf name>.mp4` | Output video file |
| `-d, --duration` | `5` | Seconds each slide is shown |
| `--durations` | — | Per-slide durations, e.g. `5,3,3,8` (overrides `-d`; last value repeats if short) |
| `-m, --music` | — | Audio file to play over the slideshow (mp3, wav, m4a, ...) |
| `--music-volume` | `1.0` | Music volume multiplier (0.0–2.0) |
| `--no-loop-music` | off | Don't loop music shorter than the video |
| `--music-fade` | `2` | Seconds of audio fade-out at the end (`0` to disable) |
| `-t, --transition` | `0.5` | Crossfade duration between slides (`0` for hard cuts) |
| `-r, --resolution` | `1080p` | `WIDTHxHEIGHT` or preset: `1080p`, `720p`, `4k`, `vertical`, `square` |
| `--fps` | `30` | Output frame rate |
| `--background` | `black` | Letterbox color (ffmpeg color name or `#RRGGBB`) |
| `--pages` | all | Pages to include, 1-based, e.g. `1-5,8,10` |
| `-q, --quiet` | off | Less output |

## Examples

```bash
# Vertical video for social media, 2s per slide, hard cuts
python pdf2video.py deck.pdf -r vertical -d 2 -t 0

# Title slide for 8s, the rest for 4s each, music kept at natural length
python pdf2video.py deck.pdf --durations 8,4 -m track.mp3 --no-loop-music

# Only pages 1–5 and 10, white letterbox, 4K
python pdf2video.py deck.pdf --pages 1-5,10 --background white -r 4k
```

## Notes

- Output is H.264 video + AAC audio in an MP4 with `+faststart`, so it plays
  everywhere (browsers, phones, social platforms).
- Crossfades are automatically capped so they always fit within the shortest
  slide duration.
- If the music is longer than the video it's trimmed; if shorter it loops
  (unless `--no-loop-music` is set). The fade-out is applied either way.
