#!/usr/bin/env python3
"""
ThrobbinJon_gui.py - "The Jon Throb Filter"

Professional desktop front-end for ThrobbinJon.py: download a YouTube video
(or load a local file), transcribe it, and mute/bleep every profane word.

Run it directly:
    python ThrobbinJon_gui.py

Requires: customtkinter, pillow  (pip install customtkinter pillow)
Everything else (yt-dlp, faster-whisper, ffmpeg) is handled by ThrobbinJon.py,
which also falls back to the imageio-ffmpeg bundled binary if ffmpeg isn't
on PATH.
"""
import json
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ThrobbinJon as core

# ---------------------------------------------------------------------------
# Brand palette
# ---------------------------------------------------------------------------
BG = "#0E1117"
SURFACE = "#161B22"
SURFACE_ALT = "#1C2330"
BORDER = "#2A3240"
ACCENT = "#3B82F6"
ACCENT_HOVER = "#2563EB"
TEXT_MAIN = "#E6E9EF"
TEXT_DIM = "#8B94A3"
GOOD = "#22C55E"
WARN = "#F59E0B"
BAD = "#EF4444"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class QueueWriter:
    """File-like object that pushes writes onto a queue instead of a real stream."""

    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(text)

    def flush(self):
        pass


class StatusPill(ctk.CTkFrame):
    """Small rounded badge used to show Idle / Running / Done / Failed."""

    STYLES = {
        "idle": (SURFACE_ALT, TEXT_DIM, "Idle"),
        "running": ("#1E3A5F", "#60A5FA", "Running..."),
        "done": ("#163C28", GOOD, "Done"),
        "failed": ("#3B1A1A", BAD, "Failed"),
    }

    def __init__(self, master):
        super().__init__(master, corner_radius=12, fg_color=SURFACE_ALT, border_width=1, border_color=BORDER)
        self.dot = ctk.CTkLabel(self, text="●", text_color=TEXT_DIM, font=ctk.CTkFont(size=13))
        self.dot.pack(side="left", padx=(12, 4), pady=6)
        self.label = ctk.CTkLabel(self, text="Idle", text_color=TEXT_DIM, font=ctk.CTkFont(size=13, weight="bold"))
        self.label.pack(side="left", padx=(0, 12), pady=6)
        self.set_state("idle")

    def set_state(self, state, text=None):
        fg, dot_color, default_text = self.STYLES[state]
        self.configure(fg_color=fg)
        self.dot.configure(text_color=dot_color)
        self.label.configure(text=text or default_text, text_color=dot_color)


class App(ctk.CTk):
    MODELS = ["tiny", "base", "small", "medium", "large-v3"]
    DEVICES = ["auto", "cpu", "cuda"]

    def __init__(self):
        super().__init__()
        self.title("The Jon Throb Filter")
        self.geometry("1040x720")
        self.minsize(900, 640)
        self.configure(fg_color=BG)

        self.log_queue = queue.Queue()
        self.worker = None

        self._build_header()
        self._build_body()
        self._poll_queue()

    # ---------------- Header ----------------

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0, height=104)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        inner = ctk.CTkFrame(header, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=28, pady=14)

        title = ctk.CTkLabel(
            inner,
            text="THE JON THROB FILTER",
            font=ctk.CTkFont(family="Segoe UI", size=30, weight="bold"),
            text_color=TEXT_MAIN,
            anchor="w",
        )
        title.pack(anchor="w")

        subtitle = ctk.CTkLabel(
            inner,
            text="Download any YouTube video and get a clean, profanity-free cut back — fast.",
            font=ctk.CTkFont(size=13),
            text_color=TEXT_DIM,
            anchor="w",
        )
        subtitle.pack(anchor="w", pady=(2, 0))

    # ---------------- Body ----------------

    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=20)
        body.grid_columnconfigure(0, weight=0)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self._build_settings_card(body)
        self._build_right_panel(body)

    # ---------------- Settings card (left) ----------------

    def _build_settings_card(self, parent):
        card = ctk.CTkScrollableFrame(
            parent, width=380, fg_color=SURFACE, corner_radius=14,
            border_width=1, border_color=BORDER, label_text="",
        )
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 16))

        pad = {"padx": 18, "pady": (6, 0)}

        def section(text):
            ctk.CTkLabel(
                card, text=text.upper(), font=ctk.CTkFont(size=11, weight="bold"),
                text_color=ACCENT,
            ).pack(anchor="w", padx=18, pady=(18, 6))

        # --- Source ---
        section("Source")
        self.source_var = tk.StringVar()
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=18)
        ctk.CTkEntry(
            row, textvariable=self.source_var, placeholder_text="YouTube URL or local file path",
            fg_color=SURFACE_ALT, border_color=BORDER, height=34,
        ).pack(side="left", fill="x", expand=True)
        self._secondary_button(row, "Browse", self._browse_source, width=78).pack(side="left", padx=(8, 0))

        # --- Output folder ---
        ctk.CTkLabel(card, text="Output folder", font=ctk.CTkFont(size=12), text_color=TEXT_DIM).pack(
            anchor="w", padx=18, pady=(14, 4)
        )
        self.outdir_var = tk.StringVar(value=".")
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=18)
        ctk.CTkEntry(
            row, textvariable=self.outdir_var, fg_color=SURFACE_ALT, border_color=BORDER, height=34,
        ).pack(side="left", fill="x", expand=True)
        self._secondary_button(row, "Browse", self._browse_outdir, width=78).pack(side="left", padx=(8, 0))

        # --- Engine ---
        section("Engine")
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=18)
        grid.grid_columnconfigure(0, weight=1)
        grid.grid_columnconfigure(1, weight=1)

        col = ctk.CTkFrame(grid, fg_color="transparent")
        col.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkLabel(col, text="Whisper model", font=ctk.CTkFont(size=12), text_color=TEXT_DIM).pack(anchor="w")
        self.model_var = tk.StringVar(value="small")
        ctk.CTkOptionMenu(
            col, values=self.MODELS, variable=self.model_var, fg_color=SURFACE_ALT,
            button_color=BORDER, button_hover_color=ACCENT, height=34,
        ).pack(fill="x", pady=(4, 0))

        col = ctk.CTkFrame(grid, fg_color="transparent")
        col.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ctk.CTkLabel(col, text="Device", font=ctk.CTkFont(size=12), text_color=TEXT_DIM).pack(anchor="w")
        self.device_var = tk.StringVar(value="auto")
        ctk.CTkOptionMenu(
            col, values=self.DEVICES, variable=self.device_var, fg_color=SURFACE_ALT,
            button_color=BORDER, button_hover_color=ACCENT, height=34,
        ).pack(fill="x", pady=(4, 0))

        # --- Filtering ---
        section("Filtering")
        ctk.CTkLabel(card, text="Mode", font=ctk.CTkFont(size=12), text_color=TEXT_DIM).pack(
            anchor="w", padx=18, pady=(0, 4)
        )
        self.mode_var = tk.StringVar(value="mute")
        seg = ctk.CTkSegmentedButton(
            card, values=["mute", "bleep"], variable=self.mode_var,
            fg_color=SURFACE_ALT, selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=SURFACE_ALT,
        )
        seg.pack(fill="x", padx=18)

        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=18, pady=(14, 0))
        grid.grid_columnconfigure(0, weight=1)
        grid.grid_columnconfigure(1, weight=1)

        col = ctk.CTkFrame(grid, fg_color="transparent")
        col.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkLabel(col, text="Padding (sec)", font=ctk.CTkFont(size=12), text_color=TEXT_DIM).pack(anchor="w")
        self.pad_var = tk.StringVar(value="0.05")
        ctk.CTkEntry(col, textvariable=self.pad_var, fg_color=SURFACE_ALT, border_color=BORDER, height=34).pack(
            fill="x", pady=(4, 0)
        )

        col = ctk.CTkFrame(grid, fg_color="transparent")
        col.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ctk.CTkLabel(col, text="Bleep volume", font=ctk.CTkFont(size=12), text_color=TEXT_DIM).pack(anchor="w")
        self.bleep_volume_var = tk.StringVar(value="0.2")
        ctk.CTkEntry(
            col, textvariable=self.bleep_volume_var, fg_color=SURFACE_ALT, border_color=BORDER, height=34,
        ).pack(fill="x", pady=(4, 0))

        self.mild_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            card, text="Also filter mild words (damn / hell / crap ...)", variable=self.mild_var,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=TEXT_MAIN, font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=18, pady=(14, 0))

        self.dry_run_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            card, text="Dry run (detect only, skip rendering)", variable=self.dry_run_var,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=TEXT_MAIN, font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=18, pady=(8, 0))

        ctk.CTkLabel(card, text="Extra word list (optional)", font=ctk.CTkFont(size=12), text_color=TEXT_DIM).pack(
            anchor="w", padx=18, pady=(14, 4)
        )
        self.words_var = tk.StringVar()
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=18)
        ctk.CTkEntry(row, textvariable=self.words_var, fg_color=SURFACE_ALT, border_color=BORDER, height=34).pack(
            side="left", fill="x", expand=True
        )
        self._secondary_button(row, "Browse", self._browse_words, width=78).pack(side="left", padx=(8, 0))

        # --- Actions ---
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(22, 18))
        self.run_btn = ctk.CTkButton(
            actions, text="▶  Run", command=self._on_run, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=14, weight="bold"), height=40,
        )
        self.run_btn.pack(fill="x")
        self._secondary_button(actions, "Open output folder", self._open_outdir, height=36).pack(
            fill="x", pady=(8, 0)
        )

    def _secondary_button(self, parent, text, command, width=None, height=34):
        kwargs = dict(
            text=text, command=command, fg_color="transparent", hover_color=SURFACE_ALT,
            border_width=1, border_color=BORDER, text_color=TEXT_MAIN, height=height,
        )
        if width:
            kwargs["width"] = width
        return ctk.CTkButton(parent, **kwargs)

    # ---------------- Right panel: status + log ----------------

    def _build_right_panel(self, parent):
        right = ctk.CTkFrame(parent, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        status_row = ctk.CTkFrame(right, fg_color="transparent")
        status_row.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        status_row.grid_columnconfigure(1, weight=1)

        self.status_pill = StatusPill(status_row)
        self.status_pill.grid(row=0, column=0, sticky="w")

        self.progress = ctk.CTkProgressBar(
            status_row, mode="indeterminate", fg_color=SURFACE_ALT, progress_color=ACCENT, height=8,
        )
        self.progress.grid(row=0, column=1, sticky="ew", padx=(16, 0))
        self.progress.set(0)

        log_card = ctk.CTkFrame(right, fg_color=SURFACE, corner_radius=14, border_width=1, border_color=BORDER)
        log_card.grid(row=1, column=0, sticky="nsew")
        log_card.grid_rowconfigure(1, weight=1)
        log_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            log_card, text="ACTIVITY LOG", font=ctk.CTkFont(size=11, weight="bold"), text_color=ACCENT,
        ).grid(row=0, column=0, sticky="w", padx=18, pady=(14, 6))

        self.log_text = ctk.CTkTextbox(
            log_card, fg_color=BG, text_color="#D8DEE9", border_width=0,
            font=ctk.CTkFont(family="Consolas", size=12), wrap="word", state="disabled",
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 18))

    # ---------------- Dialog helpers ----------------

    def _browse_source(self):
        path = filedialog.askopenfilename(
            title="Choose a video file",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.webm *.avi"), ("All files", "*.*")],
        )
        if path:
            self.source_var.set(path)

    def _browse_outdir(self):
        path = filedialog.askdirectory(title="Choose output folder")
        if path:
            self.outdir_var.set(path)

    def _browse_words(self):
        path = filedialog.askopenfilename(
            title="Choose extra word list file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.words_var.set(path)

    def _open_outdir(self):
        outdir = Path(self.outdir_var.get() or ".")
        if not outdir.exists():
            messagebox.showerror("Error", f"Folder does not exist:\n{outdir}")
            return
        import os
        import platform

        if platform.system() == "Windows":
            os.startfile(str(outdir))
        elif platform.system() == "Darwin":
            import subprocess

            subprocess.run(["open", str(outdir)])
        else:
            import subprocess

            subprocess.run(["xdg-open", str(outdir)])

    # ---------------- Run pipeline ----------------

    def _append_log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                text = self.log_queue.get_nowait()
                self._append_log(text)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _on_run(self):
        if self.worker and self.worker.is_alive():
            return

        source = self.source_var.get().strip()
        if not source:
            messagebox.showerror("Error", "Please provide a YouTube URL or a local file path.")
            return

        ffmpeg_path = core.resolve_ffmpeg()
        if not ffmpeg_path:
            messagebox.showerror(
                "Error",
                "ffmpeg not found on PATH and the imageio-ffmpeg fallback isn't installed.\n"
                "Run: pip install imageio-ffmpeg",
            )
            return

        try:
            pad = float(self.pad_var.get())
            bleep_volume = float(self.bleep_volume_var.get())
        except ValueError:
            messagebox.showerror("Error", "Padding and bleep volume must be numbers.")
            return

        words_file = self.words_var.get().strip()
        if words_file and not Path(words_file).exists():
            messagebox.showerror("Error", f"Word list file not found:\n{words_file}")
            return

        options = {
            "source": source,
            "outdir": self.outdir_var.get().strip() or ".",
            "model": self.model_var.get(),
            "device": self.device_var.get(),
            "mode": self.mode_var.get(),
            "pad": pad,
            "mild": self.mild_var.get(),
            "words": words_file or None,
            "bleep_volume": bleep_volume,
            "dry_run": self.dry_run_var.get(),
            "ffmpeg_path": ffmpeg_path,
        }

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

        self.run_btn.configure(state="disabled")
        self.status_pill.set_state("running")
        self.progress.start()

        self.worker = threading.Thread(target=self._run_pipeline, args=(options,), daemon=True)
        self.worker.start()

    def _run_pipeline(self, options):
        writer = QueueWriter(self.log_queue)
        old_stdout = sys.stdout
        sys.stdout = writer
        error = None
        try:
            self._pipeline(options)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the GUI
            error = exc
        finally:
            sys.stdout = old_stdout

        self.after(0, self._on_finished, error)

    def _pipeline(self, options):
        outdir = Path(options["outdir"])
        outdir.mkdir(parents=True, exist_ok=True)

        roots, exact = set(core.ROOTS), set(core.EXACT)
        if options["mild"]:
            exact |= core.MILD
        if options["words"]:
            r, e = core.load_custom_words(options["words"])
            roots |= r
            exact |= e

        src = Path(options["source"])
        if not src.exists():
            print("Downloading ...")
            src = core.download(options["source"], outdir, options["ffmpeg_path"])
        print(f"Source: {src}")

        hits = core.find_profanity(
            src, options["model"], options["device"], roots, exact, options["ffmpeg_path"]
        )
        intervals = core.merge_intervals(hits, options["pad"])
        print(f"\nFound {len(hits)} word(s) in {len(intervals)} range(s).")

        report = outdir / f"{src.stem}.clean.json"
        report.write_text(json.dumps({"hits": hits, "intervals": intervals}, indent=2))
        print(f"Report: {report}")

        if options["dry_run"]:
            return

        dst = outdir / f"{src.stem}.clean.mp4"
        print(f"Rendering ({options['mode']}) ...")
        core.render(src, dst, intervals, options["mode"], options["bleep_volume"], options["ffmpeg_path"])
        print(f"Done: {dst}")

    def _on_finished(self, error):
        self.progress.stop()
        self.progress.set(0)
        self.run_btn.configure(state="normal")
        if error:
            self.status_pill.set_state("failed")
            self._append_log(f"\nERROR: {error}\n")
            messagebox.showerror("Error", str(error))
        else:
            self.status_pill.set_state("done")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
