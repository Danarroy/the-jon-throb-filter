# The Jon Throb Filter

Download a YouTube video (or point it at a local file) and get back a clean,
profanity-free cut — fast.

## How it works

1. [yt-dlp](https://github.com/yt-dlp/yt-dlp) downloads the video.
2. [faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes it
   with word-level timestamps, using voice-activity detection to skip silence.
3. Transcribed words are matched against a profanity list.
4. `ffmpeg` mutes (or bleeps) just those moments — the video stream is copied,
   never re-encoded, so rendering is quick.

## Just want to run it?

Grab the latest `.exe` from the [Releases](../../releases) page — no Python
or ffmpeg install required. Unzip it and run `TheJonThrobFilter.exe` (keep it
next to the `_internal` folder it comes with).

## Running from source

```
pip install -r requirements.txt
python ThrobbinJon_gui.py
```

Or use the command line directly:

```
python ThrobbinJon.py "https://www.youtube.com/watch?v=XXXX"
python ThrobbinJon.py URL --mode bleep --model medium
python ThrobbinJon.py myvideo.mp4 --mild --pad 0.1
python ThrobbinJon.py URL --dry-run            # just list what would be muted
```

ffmpeg is picked up automatically — it uses a system install if you have one
on PATH, otherwise it falls back to the bundled `imageio-ffmpeg` binary.
