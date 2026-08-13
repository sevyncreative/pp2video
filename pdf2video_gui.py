#!/usr/bin/env python3
"""Graphical interface for pdf2video.

Runs pdf2video.py (which must sit next to this file) as a subprocess, so the
GUI stays responsive and the CLI remains the single source of truth for the
conversion logic. Requires only the Python standard library (tkinter).

Usage:  python pdf2video_gui.py
"""

from __future__ import annotations

import os
import platform
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

SCRIPT = Path(__file__).resolve().parent / "pdf2video.py"

RESOLUTIONS = ["1080p", "720p", "4k", "vertical", "square"]

AUDIO_FILETYPES = [
    ("Audio files", "*.mp3 *.wav *.m4a *.aac *.ogg *.opus *.flac *.wma"),
    ("All files", "*.*"),
]

TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
TOTAL_RE = re.compile(r"Encoding (\d+(?:\.\d+)?)s video")
PART_RE = re.compile(r"part (\d+)/(\d+) \(starts at (\d+(?:\.\d+)?)s\)")


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("PDF → Video")
        root.minsize(560, 520)

        self.process: subprocess.Popen | None = None
        self.output_queue: queue.Queue[str | None] = queue.Queue()
        self.total_seconds = 0.0
        self.last_output: Path | None = None

        pad = {"padx": 8, "pady": 4}
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        # --- Files ---
        files = ttk.LabelFrame(frame, text="Files", padding=8)
        files.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
        files.columnconfigure(1, weight=1)

        self.pdf_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.music_tracks: list[str] = []

        ttk.Label(files, text="PDF slideshow:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.pdf_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(files, text="Browse…", command=self.pick_pdf).grid(row=0, column=2, **pad)

        ttk.Label(files, text="Save video as:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(files, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(files, text="Browse…", command=self.pick_output).grid(row=1, column=2, **pad)

        ttk.Label(files, text="Music (optional,\nplays in order):").grid(
            row=2, column=0, sticky="nw", **pad)
        self.track_list = tk.Listbox(files, height=4, activestyle="dotbox")
        self.track_list.grid(row=2, column=1, rowspan=4, sticky="nsew", **pad)
        ttk.Button(files, text="Add…", command=self.add_music).grid(
            row=2, column=2, sticky="ew", **pad)
        ttk.Button(files, text="Remove", command=self.remove_music).grid(
            row=3, column=2, sticky="ew", **pad)
        ttk.Button(files, text="Move up", command=lambda: self.move_music(-1)).grid(
            row=4, column=2, sticky="ew", **pad)
        ttk.Button(files, text="Move down", command=lambda: self.move_music(1)).grid(
            row=5, column=2, sticky="ew", **pad)

        # --- Slides ---
        slides = ttk.LabelFrame(frame, text="Slides", padding=8)
        slides.grid(row=1, column=0, sticky="nsew", **pad)

        self.duration_var = tk.StringVar(value="5")
        self.transition_var = tk.StringVar(value="0.5")
        self.resolution_var = tk.StringVar(value="1080p")
        self.pages_var = tk.StringVar()

        ttk.Label(slides, text="Seconds per slide:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Spinbox(slides, from_=0.5, to=600, increment=0.5, width=7,
                    textvariable=self.duration_var).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(slides, text="Crossfade (s):").grid(row=1, column=0, sticky="w", **pad)
        ttk.Spinbox(slides, from_=0, to=10, increment=0.25, width=7,
                    textvariable=self.transition_var).grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(slides, text="Resolution:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Combobox(slides, values=RESOLUTIONS, width=10,
                     textvariable=self.resolution_var).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(slides, text="Pages (e.g. 1-5,8):").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(slides, textvariable=self.pages_var, width=12).grid(row=3, column=1, sticky="w", **pad)

        # --- Music ---
        music = ttk.LabelFrame(frame, text="Music", padding=8)
        music.grid(row=1, column=1, sticky="nsew", **pad)
        music.columnconfigure(1, weight=1)

        self.volume_var = tk.DoubleVar(value=100.0)
        self.loop_var = tk.BooleanVar(value=True)
        self.fade_var = tk.StringVar(value="2")

        ttk.Label(music, text="Volume:").grid(row=0, column=0, sticky="w", **pad)
        self.volume_label = ttk.Label(music, text="100%", width=5)
        self.volume_label.grid(row=0, column=2, sticky="e", **pad)
        ttk.Scale(music, from_=0, to=200, variable=self.volume_var,
                  command=self.on_volume_change).grid(row=0, column=1, sticky="ew", **pad)

        ttk.Checkbutton(music, text="Loop playlist to fill the video",
                        variable=self.loop_var).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(music, text="Fade out (s):").grid(row=2, column=0, sticky="w", **pad)
        ttk.Spinbox(music, from_=0, to=30, increment=0.5, width=7,
                    textvariable=self.fade_var).grid(row=2, column=1, sticky="w", **pad)

        # --- Actions ---
        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)
        actions.columnconfigure(0, weight=1)

        self.convert_button = ttk.Button(actions, text="Create video", command=self.start)
        self.convert_button.grid(row=0, column=0, sticky="ew", **pad)
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_button.grid(row=0, column=1, **pad)
        self.open_button = ttk.Button(actions, text="Open folder", command=self.open_folder,
                                      state="disabled")
        self.open_button.grid(row=0, column=2, **pad)

        self.progress = ttk.Progressbar(frame, maximum=100)
        self.progress.grid(row=3, column=0, columnspan=2, sticky="ew", **pad)

        self.status_var = tk.StringVar(value="Choose a PDF to get started.")
        ttk.Label(frame, textvariable=self.status_var, anchor="w").grid(
            row=4, column=0, columnspan=2, sticky="ew", **pad)

        self.log = tk.Text(frame, height=8, state="disabled", wrap="word")
        self.log.grid(row=5, column=0, columnspan=2, sticky="nsew", **pad)
        frame.rowconfigure(5, weight=1)

        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ----- pickers -----

    def pick_pdf(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose PDF slideshow",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if path:
            self.pdf_var.set(path)
            if not self.output_var.get():
                self.output_var.set(str(Path(path).with_suffix(".mp4")))
            self.status_var.set("Ready.")

    def add_music(self) -> None:
        paths = filedialog.askopenfilenames(title="Choose music track(s)",
                                            filetypes=AUDIO_FILETYPES)
        for path in paths:
            self.music_tracks.append(path)
            self.track_list.insert("end", Path(path).name)

    def remove_music(self) -> None:
        selection = self.track_list.curselection()
        if not selection:
            return
        index = selection[0]
        del self.music_tracks[index]
        self.track_list.delete(index)
        if self.music_tracks:
            self.track_list.selection_set(min(index, len(self.music_tracks) - 1))

    def move_music(self, delta: int) -> None:
        selection = self.track_list.curselection()
        if not selection:
            return
        i, j = selection[0], selection[0] + delta
        if not 0 <= j < len(self.music_tracks):
            return
        self.music_tracks[i], self.music_tracks[j] = self.music_tracks[j], self.music_tracks[i]
        self.track_list.delete(i)
        self.track_list.insert(j, Path(self.music_tracks[j]).name)
        self.track_list.selection_set(j)
        self.track_list.see(j)

    def pick_output(self) -> None:
        initial = self.output_var.get() or "slideshow.mp4"
        path = filedialog.asksaveasfilename(
            title="Save video as", defaultextension=".mp4",
            initialfile=Path(initial).name,
            filetypes=[("MP4 video", "*.mp4"), ("All files", "*.*")])
        if path:
            self.output_var.set(path)

    def on_volume_change(self, _value: str) -> None:
        self.volume_label.config(text=f"{self.volume_var.get():.0f}%")

    # ----- conversion -----

    def build_command(self) -> list[str] | None:
        pdf = self.pdf_var.get().strip()
        if not pdf:
            messagebox.showwarning("Missing PDF", "Choose a PDF slideshow first.")
            return None
        if not Path(pdf).is_file():
            messagebox.showerror("Not found", f"PDF not found:\n{pdf}")
            return None

        cmd = [sys.executable, str(SCRIPT), pdf]

        output = self.output_var.get().strip()
        if output:
            cmd += ["-o", output]
            self.last_output = Path(output)
        else:
            self.last_output = Path(pdf).with_suffix(".mp4")

        try:
            duration = float(self.duration_var.get())
            transition = float(self.transition_var.get())
            fade = float(self.fade_var.get())
        except ValueError:
            messagebox.showerror("Invalid value",
                                 "Durations, crossfade and fade-out must be numbers.")
            return None

        cmd += ["-d", str(duration), "-t", str(transition),
                "-r", self.resolution_var.get().strip() or "1080p"]

        pages = self.pages_var.get().strip()
        if pages:
            cmd += ["--pages", pages]

        if self.music_tracks:
            for track in self.music_tracks:
                if not Path(track).is_file():
                    messagebox.showerror("Not found", f"Music file not found:\n{track}")
                    return None
                cmd += ["-m", track]
            cmd += ["--music-volume", f"{self.volume_var.get() / 100:.2f}",
                    "--music-fade", str(fade)]
            if not self.loop_var.get():
                cmd.append("--no-loop-music")
        return cmd

    def start(self) -> None:
        if self.process is not None:
            return
        cmd = self.build_command()
        if cmd is None:
            return

        self.total_seconds = 0.0
        self.base_seconds = 0.0
        self.finishing = False
        self.progress.config(value=0)
        self.set_log("")
        self.status_var.set("Working…")
        self.convert_button.config(state="disabled")
        self.cancel_button.config(state="normal")
        self.open_button.config(state="disabled")

        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            self.finish(error=f"Could not start converter: {exc}")
            return
        threading.Thread(target=self.pump_output, daemon=True).start()
        self.root.after(100, self.poll_queue)

    def pump_output(self) -> None:
        """Read subprocess output (ffmpeg uses \\r for progress) into the queue."""
        assert self.process is not None and self.process.stdout is not None
        buffer = b""
        while True:
            chunk = self.process.stdout.read(256)
            if not chunk:
                break
            buffer += chunk
            while True:
                cut = min((i for i in (buffer.find(b"\n"), buffer.find(b"\r")) if i != -1),
                          default=-1)
                if cut == -1:
                    break
                line, buffer = buffer[:cut], buffer[cut + 1:]
                if line.strip():
                    self.output_queue.put(line.decode("utf-8", "replace"))
        if buffer.strip():
            self.output_queue.put(buffer.decode("utf-8", "replace"))
        self.process.wait()
        self.output_queue.put(None)  # sentinel: process finished

    def poll_queue(self) -> None:
        finished = False
        while True:
            try:
                item = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                finished = True
                break
            self.handle_line(item)
        if finished:
            code = self.process.returncode if self.process else -1
            self.process = None
            if code == 0:
                self.finish()
            else:
                self.finish(error=f"Conversion failed (exit code {code}) — see log below.")
        elif self.process is not None:
            self.root.after(100, self.poll_queue)

    def handle_line(self, line: str) -> None:
        match = TOTAL_RE.search(line)
        if match:
            self.total_seconds = float(match.group(1))
        match = PART_RE.search(line)
        if match:
            self.base_seconds = float(match.group(3))
        if "Combining parts" in line:
            # Stitching pass: fast stream copy whose time= restarts from zero.
            self.finishing = True
            self.status_var.set("Finishing…")
        match = TIME_RE.search(line)
        if match and self.total_seconds > 0:
            if self.finishing:
                return
            h, m, s = match.groups()
            done = self.base_seconds + int(h) * 3600 + int(m) * 60 + float(s)
            percent = min(100.0, done / self.total_seconds * 100)
            self.progress.config(value=percent)
            self.status_var.set(f"Encoding… {percent:.0f}%")
        elif not line.startswith("frame="):
            self.append_log(line)
            if line.startswith("Rendering"):
                self.status_var.set(line)

    def finish(self, error: str | None = None) -> None:
        self.convert_button.config(state="normal")
        self.cancel_button.config(state="disabled")
        if error:
            self.status_var.set(error)
        else:
            self.progress.config(value=100)
            self.status_var.set(f"Done: {self.last_output}")
            self.open_button.config(state="normal")

    def cancel(self) -> None:
        if self.process is not None:
            self.process.terminate()
            self.status_var.set("Cancelled.")

    def open_folder(self) -> None:
        if self.last_output is None:
            return
        folder = str(self.last_output.resolve().parent)
        system = platform.system()
        if system == "Windows":
            os.startfile(folder)  # type: ignore[attr-defined]
        elif system == "Darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])

    def on_close(self) -> None:
        if self.process is not None:
            if not messagebox.askokcancel("Quit", "A conversion is running. Stop it and quit?"):
                return
            self.process.terminate()
        self.root.destroy()

    # ----- log helpers -----

    def set_log(self, text: str) -> None:
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("1.0", text)
        self.log.config(state="disabled")

    def append_log(self, line: str) -> None:
        self.log.config(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.config(state="disabled")


def main() -> None:
    if not SCRIPT.is_file():
        sys.exit(f"error: {SCRIPT} not found — keep pdf2video_gui.py next to pdf2video.py")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
