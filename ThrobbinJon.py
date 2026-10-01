#!/usr/bin/env python3
"""
cleancut.py - download a YouTube video (or use a local file) and mute the curse words.

Pipeline:
  1. yt-dlp downloads the video as mp4
  2. faster-whisper transcribes it with word-level timestamps
  3. words are matched against a profanity list
  4. ffmpeg mutes (or bleeps) those time ranges; video stream is copied, not re-encoded

Setup:
  pip install yt-dlp faster-whisper
  ffmpeg must be on your PATH

Examples:
  python cleancut.py "https://www.youtube.com/watch?v=XXXX"
  python cleancut.py URL --mode bleep --model medium
  python cleancut.py myvideo.mp4 --mild --pad 0.1
  python cleancut.py URL --dry-run            # just list what would be muted
  python cleancut.py URL --words extra.txt    # add your own words
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Windows consoles default to a legacy codepage (cp1252 etc.) that can't print
# every character yt-dlp puts in a video title (fullwidth punctuation, emoji, ...).
# Widen stdout/stderr to UTF-8 so a fancy title never crashes a print().
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Substring roots: catch every variant ("fucking", "motherfucker", "bullshit", ...)
ROOTS = {"fuck", "shit", "bitch", "cunt"}

# Exact matches: short words that would cause false positives as substrings
# ("ass" would hit "class", "dick" would hit "Dickens", etc.)
EXACT = {
    "ass", "asses", "asshole", "assholes", "jackass", "dumbass", "badass",
    "dick", "dicks", "dickhead", "cock", "cocks", "prick", "pricks",
    "piss", "pissed", "pissing", "bastard", "bastards",
    "twat", "wanker", "bollocks", "goddamn", "goddamnit", "goddammit",
}

# Milder words, only filtered with --mild
MILD = {"damn", "damned", "dammit", "damnit", "hell", "crap", "crappy", "bloody"}


def load_custom_words(path):
    """One word per line. Prefix with ~ to make it a substring root. # for comments."""
    roots, exact = set(), set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip().lower()
        if not line or line.startswith("#"):
            continue
        if line.startswith("~"):
            roots.add(line[1:])
        else:
            exact.add(line)
    return roots, exact


def normalize(word):
    # keep letters and '*' (Whisper sometimes writes "f***")
    return re.sub(r"[^a-z*]", "", word.lower())


def is_profane(token, roots, exact):
    if not token:
        return False
    if "*" in token:  # already-censored transcription
        return True
    return token in exact or any(r in token for r in roots)


def resolve_ffmpeg():
    """Prefer a system ffmpeg on PATH; fall back to the one bundled by imageio-ffmpeg."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def download(url, outdir, ffmpeg_path=None):
    import yt_dlp

    opts = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(outdir / "%(title).80s [%(id)s].%(ext)s"),
        "noplaylist": True,
        # speed: pull multiple fragments of the stream at once instead of serially
        "concurrent_fragment_downloads": 8,
    }
    if ffmpeg_path:
        opts["ffmpeg_location"] = ffmpeg_path
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        dl = info.get("requested_downloads") or []
        if dl and dl[0].get("filepath"):
            return Path(dl[0]["filepath"])
        return Path(ydl.prepare_filename(info)).with_suffix(".mp4")


_MODEL_CACHE = {}


def _get_model(model_name, device):
    """Reuse an already-loaded model within the same process (big win when
    cleaning several videos back to back in the GUI)."""
    key = (model_name, device)
    if key not in _MODEL_CACHE:
        from faster_whisper import WhisperModel

        print(f"Loading Whisper model '{model_name}' ...")
        _MODEL_CACHE[key] = WhisperModel(model_name, device=device, compute_type="auto", cpu_threads=0)
    return _MODEL_CACHE[key]


def _decode_audio(media, ffmpeg_path, sample_rate=16000):
    """Decode to mono 16kHz PCM via ffmpeg and hand faster-whisper a numpy array
    directly. This sidesteps faster-whisper's own PyAV-based decode_audio(), whose
    av.open(metadata_errors=...) call breaks on current PyAV releases."""
    import numpy as np

    cmd = [ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error",
           "-i", str(media), "-f", "s16le", "-ac", "1", "-ar", str(sample_rate), "-"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, check=True)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def find_profanity(media, model_name, device, roots, exact, ffmpeg_path):
    model = _get_model(model_name, device)
    print("Transcribing (this is the slow part) ...")
    audio = _decode_audio(media, ffmpeg_path)
    # vad_filter skips silence/non-speech instead of decoding it, which is the single
    # biggest speedup available here and never affects which spoken words get caught
    segments, info = model.transcribe(
        audio,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 300},
        beam_size=1 if model_name in ("tiny", "base") else 5,
    )

    hits = []
    for seg in segments:
        for w in seg.words or []:
            if is_profane(normalize(w.word), roots, exact):
                hits.append({"word": w.word.strip(), "start": w.start, "end": w.end,
                             "prob": round(w.probability, 3)})
                print(f"  [{w.start:8.2f} - {w.end:8.2f}]  {w.word.strip()}")
    return hits


def merge_intervals(hits, pad):
    spans = sorted((max(0.0, h["start"] - pad), h["end"] + pad) for h in hits)
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def render(src, dst, intervals, mode, bleep_volume, ffmpeg_path="ffmpeg"):
    base = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    audio_out = ["-c:a", "aac", "-b:a", "192k"]

    if not intervals:
        cmd = base + ["-c", "copy", str(dst)]
    else:
        cond = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in intervals)
        if mode == "mute":
            cmd = base + ["-map", "0:v?", "-map", "0:a", "-c:v", "copy",
                          "-af", f"volume=0:enable='{cond}'"] + audio_out + [str(dst)]
        else:  # bleep: mute speech, and let a 1 kHz tone through only during hits
            fc = (
                f"[0:a]volume=0:enable='{cond}'[m];"
                f"sine=frequency=1000:sample_rate=48000,"
                f"volume=0:enable='not({cond})',volume={bleep_volume}[b];"
                f"[m][b]amix=inputs=2:duration=first:normalize=0[a]"
            )
            cmd = base + ["-filter_complex", fc, "-map", "0:v?", "-map", "[a]",
                          "-c:v", "copy"] + audio_out + [str(dst)]

    subprocess.run(cmd, check=True)


def main():
    p = argparse.ArgumentParser(description="Download a video and mute the profanity.")
    p.add_argument("source", help="YouTube URL or path to a local video file")
    p.add_argument("-o", "--outdir", default=".", help="output folder (default: current)")
    p.add_argument("--model", default="small",
                   help="Whisper model: tiny, base, small, medium, large-v3 (bigger = more accurate, slower)")
    p.add_argument("--device", default="auto", help="auto, cpu, or cuda")
    p.add_argument("--mode", choices=["mute", "bleep"], default="mute")
    p.add_argument("--pad", type=float, default=0.05,
                   help="seconds of extra silence on each side of a word (default 0.05)")
    p.add_argument("--mild", action="store_true", help="also filter damn/hell/crap/etc.")
    p.add_argument("--words", help="extra word list file (prefix a line with ~ for substring match)")
    p.add_argument("--bleep-volume", type=float, default=0.2)
    p.add_argument("--dry-run", action="store_true", help="only list detected words, don't render")
    args = p.parse_args()

    ffmpeg_path = resolve_ffmpeg()
    if not ffmpeg_path:
        sys.exit("ffmpeg not found on PATH and imageio-ffmpeg isn't installed. "
                  "Install ffmpeg or run: pip install imageio-ffmpeg")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    roots, exact = set(ROOTS), set(EXACT)
    if args.mild:
        exact |= MILD
    if args.words:
        r, e = load_custom_words(args.words)
        roots |= r
        exact |= e

    src = Path(args.source)
    if not src.exists():
        print("Downloading ...")
        src = download(args.source, outdir, ffmpeg_path)
    print(f"Source: {src}")

    hits = find_profanity(src, args.model, args.device, roots, exact, ffmpeg_path)
    intervals = merge_intervals(hits, args.pad)
    print(f"\nFound {len(hits)} word(s) in {len(intervals)} range(s).")

    report = outdir / f"{src.stem}.clean.json"
    report.write_text(json.dumps({"hits": hits, "intervals": intervals}, indent=2))
    print(f"Report: {report}")

    if args.dry_run:
        return

    dst = outdir / f"{src.stem}.clean.mp4"
    print(f"Rendering ({args.mode}) ...")
    render(src, dst, intervals, args.mode, args.bleep_volume, ffmpeg_path)
    print(f"Done: {dst}")


if __name__ == "__main__":
    main()