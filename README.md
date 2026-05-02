# Fruitpicker

Automates Apple Music playback in a browser and records each track to a separate MP3.

## What It Does

- Automates Apple Music navigation with Selenium
- Captures system output audio (not microphone) on Linux via PulseAudio monitor sources
- Splits songs by title change in the player DOM
- Encodes to MP3 with `ffmpeg` and writes ID3 title/artist tags
- Stops at playlist end when playback returns to the LCD Play state

## Quick Start

```bash
uv run apple_music_recorder.py --playlist "https://music.apple.com/playlist/..."
```

This will use the config.json defaults.

Useful options:

```bash
python apple_music_recorder.py --playlist "https://music.apple.com/playlist/..."
python apple_music_recorder.py --max-songs 10
python apple_music_recorder.py --output ./my-recordings
python apple_music_recorder.py --headless
python apple_music_recorder.py --browser firefox
python apple_music_recorder.py --bitrate 320k
python apple_music_recorder.py --log-level DEBUG
python apple_music_recorder.py --dry-run
```

## Config Schema
Filename placeholders supported by `output.naming_convention`:

- `{track_number}`
- `{artist}`
- `{title}`
- `{timestamp}`

## Linux Audio Capture Notes

On Linux, the recorder:

1. Reads default sink from `pactl info`
2. Selects `<default-sink>.monitor` source
3. Sets `PULSE_SOURCE` to that monitor
4. Uses sounddevice `pulse` input device when available

If monitor detection fails, it falls back to default input (which may be a microphone).

## How Song Boundaries Work

- The tool reads now-playing title/artist from Apple Music LCD shadow DOM.
- When title changes, it closes current recording and starts a new one.
- At playlist end, when Play (not Pause) is active in LCD controls for several polls, the recorder stops and exits.

## Troubleshooting

### Capturing microphone instead of system audio

```bash
pactl info | grep "Default Sink"
pactl list sources short
```

Ensure a `.monitor` source exists for your default sink.

### Song/title detection drift after Apple Music UI changes

- Run with `--log-level DEBUG`
- Re-check selectors in `apple_music_recorder.py` (`_detect_song_change`, `_is_lcd_showing_play_button`)

## Programmatic Use

```python
from apple_music_recorder import AppleMusicRecorder

recorder = AppleMusicRecorder("config.json")
recorder.config["playlist_url"] = "https://music.apple.com/playlist/..."
recorder.config["output"]["max_songs"] = 5


def on_song_start(count, info):
    print(count, info.get("artist"), info.get("title"))


recorder.on_song_start = on_song_start
recorder.run()
```

## Disclaimer

Personal-use tool. Respect Apple Music terms and local copyright laws.
