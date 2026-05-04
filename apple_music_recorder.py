#!/usr/bin/env python3
"""
Apple Music Recorder
====================
A Python script that automates browser navigation through Apple Music
and records each song as a separate MP3 file using system audio capture.

Requirements:
    pip install selenium sounddevice numpy mutagen
    sudo apt install pulseaudio-utils  # Linux
    brew install ffmpeg                # macOS

Usage:
    python apple_music_recorder.py                  # Use default config
    python apple_music_recorder.py --config my.json # Use custom config
    python apple_music_recorder.py --playlist URL   # Override playlist URL

Author: Apple Music Recorder
License: MIT
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread

# Try to import ffmpeg for mp3 encoding
import ffmpeg
import sounddevice as sd
from mutagen.id3 import ID3, TIT2, TPE1, ID3NoHeaderError
from selenium import webdriver
from selenium.webdriver import ActionChains, Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.remote.webelement import WebElement


class AppleMusicRecorder:
    """Main class for recording Apple Music songs."""

    def __init__(self, config_path: str = "config.json"):
        """Initialize the recorder with configuration."""
        self.config = self._load_config(config_path)
        self._setup_logging()
        self.logger = logging.getLogger("AppleMusicRecorder")

        # Recording state
        self.is_recording = False
        self.stop_event = Event()
        self._audio_lock = Lock()
        self.current_recording = None
        self.recorded_songs = []

        # Browser state
        self.driver = None
        self.song_count = 0
        self._play_button_visible_streak = 0

        # Audio state
        self.audio_stream = None
        self.audio_buffer = []
        self.recording_start_time = None

        # Callbacks
        self.on_song_start = None
        self.on_error = None

        # Setup signal handlers for graceful shutdown
        self._setup_signal_handlers()

    def _load_config(self, config_path: str) -> dict:
        """Load configuration from JSON file."""
        default_config = {
            "apple_music_url": "https://music.apple.com",
            "playlist_url": None,
            "browser": {
                "type": "chrome",
                "headless": False,
                "window_size": [1920, 1080]
            },
            "recording": {
                "sample_rate": 44100,
                "channels": 2,
                "chunk_duration": 0.1
            },
            "output": {
                "directory": "./recordings",
                "bitrate": "320k",
                "naming_convention": "{artist}_{title}",
                "max_songs": -1
            },
            "navigation": {
                "track_list_item_selector": "[data-testid='track-list-item']"
            },
            "logging": {
                "level": "INFO",
                "log_file": "./recorder.log",
                "log_format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            }
        }

        try:
            with open(config_path, 'r') as f:
                user_config = json.load(f)
            # Merge configs (user config overrides defaults)
            for key in default_config:
                if key in user_config:
                    if isinstance(default_config[key], dict):
                        default_config[key].update(user_config[key])
                    else:
                        default_config[key] = user_config[key]
        except FileNotFoundError:
            self.logger.warning(f"Config file '{config_path}' not found. Using defaults.")
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid JSON in config file: {e}. Using defaults.")

        return default_config

    def _setup_logging(self):
        """Setup logging configuration."""
        log_config = self.config.get("logging", {})
        log_level = getattr(logging, log_config.get("level", "INFO").upper())
        log_format = log_config.get("log_format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s")

        # Root logger
        logging.basicConfig(level=log_level, format=log_format)

        # File handler
        log_file = log_config.get("log_file", "./recorder.log")
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel("DEBUG")
        file_handler.setFormatter(logging.Formatter(log_format))
        logging.getLogger().addHandler(file_handler)

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""

        def signal_handler(signum, frame):
            self.logger.info(f"Received signal {signum}. Shutting down gracefully...")
            self.stop()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def _get_user_data_dir(self) -> str:
        """Get the path to the persistent user data directory for browser profile."""
        # Use a fixed directory in the project to store browser profile data (cookies, login state)
        base_dir = Path(os.path.dirname(os.path.abspath(__file__)))
        user_data_dir = base_dir / "browser_profile" / "chrome"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        return str(user_data_dir)

    def _setup_browser(self):
        """Initialize the browser for automation."""
        # if not HAS_SELENIUM:
        #     raise ImportError("Selenium is required for browser automation. Install with: pip install selenium")

        browser_config = self.config.get("browser", {})
        browser_type = browser_config.get("type", "chrome").lower()
        headless = browser_config.get("headless", False)
        window_size = browser_config.get("window_size", [1920, 1080])

        if browser_type == "chrome":
            options = Options()
            if headless:
                options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size={}".format(f"{window_size[0]},{window_size[1]}"))
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-popup-blocking")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)

            # Add flags to ensure cookies and local storage are preserved
            options.add_argument("--disable-profile-average")
            options.add_argument("--disable-backgrounding-occluded-windows")
            options.add_argument("--disable-renderer-backgrounding")
            options.add_argument("--force-renderer-affinity")

            # Persist login state across runs using a persistent user data directory
            user_data_dir = self._get_user_data_dir()
            options.add_argument(f"--user-data-dir={user_data_dir}")
            options.add_argument("--profile-directory=Default")

            # Try to find chromedriver in common locations
            chromedriver_paths = [
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "chromedriver"),
                "/usr/bin/chromedriver",
                "/usr/local/bin/chromedriver",
                "/opt/homebrew/bin/chromedriver",
            ]
            chromedriver_path = None
            for path in chromedriver_paths:
                if os.path.exists(path):
                    chromedriver_path = path
                    break

            if chromedriver_path:
                service = webdriver.ChromeService(executable_path=chromedriver_path)
                self.driver = webdriver.Chrome(service=service, options=options)
            else:
                self.driver = webdriver.Chrome(options=options)
        elif browser_type == "firefox":
            options = FirefoxOptions()
            if headless:
                options.add_argument("--headless")
            options.set_preference("browser.window.size", f"{window_size[0]},{window_size[1]}")

            self.driver = webdriver.Firefox(options=options)
        else:
            raise ValueError(f"Unsupported browser type: {browser_type}")

        self.driver.set_window_size(*window_size)
        self.logger.info(f"Browser initialized: {browser_type}, headless={headless}")

    def _navigate_to_music(self):
        """Navigate to Apple Music and ensure user is logged in."""
        playlist_url = self.config.get("playlist_url")
        url = playlist_url or self.config.get("apple_music_url", "https://music.apple.com")

        self.logger.info(f"Navigating to: {url}")
        self.driver.get(url)

        # Wait for page to load
        time.sleep(5)

        # Check if we're logged in by looking for playlist content
        # Apple Music shows a login prompt when not authenticated
        logged_in = self._check_login_status()

        if not logged_in:
            self.logger.warning("Not logged in to Apple Music. Please login manually in the browser window.")
            self.logger.info("After logging in, press Enter to continue...")
            try:
                input()
            except EOFError:
                pass
            # Wait again after manual login
            time.sleep(5)
            logged_in = self._check_login_status()
            if not logged_in:
                self.logger.error("Still not logged in after manual login attempt. Aborting.")
                return False

        self.logger.info("Page loaded successfully and user is authenticated")
        return True

    def _check_login_status(self) -> bool:
        """Check if the user is logged in to Apple Music."""
        try:
            # Check for login prompt/overlay (indicates not logged in)
            login_prompts = self.driver.find_elements(By.CSS_SELECTOR,
                                                      "[data-testid='login-button'], [class*='login'], [class*='sign-in'], "
                                                      "a[href*='login'], a[href*='signin'], button[class*='login']")

            # Check for content (indicates logged in)
            play_buttons = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='play-button']")
            song_titles = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='track-title']")
            playlist_content = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='track-list-item']")

            # If we see login prompts AND no content, user is not logged in
            if login_prompts and not play_buttons and not song_titles and not playlist_content:
                return False

            # If we see content (play buttons, song titles, or track list items), user is logged in
            if play_buttons or song_titles or playlist_content:
                return True

            # Check URL for login redirects
            current_url = self.driver.current_url
            if 'login' in current_url.lower() or 'signin' in current_url.lower():
                return False

            return True
        except Exception as e:
            self.logger.debug(f"Error checking login status: {e}")
            return False

    def _get_current_song_info(self) -> dict:
        """Detect the current song information from the DOM.

        The player LCD title/artist live inside the shadow DOM of <amp-lcd>, so we
        use JavaScript to pierce it rather than regular CSS selectors.

        DOM path:
          [data-testid='player-lcd'] amp-lcd  (shadow host)
            └─ shadowRoot
                 └─ .lcd-meta__primary  amp-marquee-text
                      └─ .lcd-meta-line__fragment[tabindex="0"]  ← title text
                 └─ .lcd-meta__secondary  amp-marquee-text
                      └─ .lcd-meta-line__fragment[tabindex="0"]  ← "Artist — Album"
        """
        nav_config = self.config.get("navigation", {})
        song_info = {"title": None, "artist": None, "track_number": None}

        try:
            # Pierce the amp-lcd shadow root via JavaScript
            title = self.driver.execute_script("""
                try {
                    const lcdHost = document.querySelector('[data-testid="player-lcd"] amp-lcd');
                    if (!lcdHost || !lcdHost.shadowRoot) return null;
                    const frag = lcdHost.shadowRoot.querySelector(
                        '.lcd-meta__primary .lcd-meta-line__fragment[tabindex="0"]'
                    );
                    return frag ? frag.textContent.trim() : null;
                } catch (e) { return null; }
            """)

            artist_raw = self.driver.execute_script("""
                try {
                    const lcdHost = document.querySelector('[data-testid="player-lcd"] amp-lcd');
                    if (!lcdHost || !lcdHost.shadowRoot) return null;
                    // Collect all fragments in the secondary line (artist — album)
                    const frags = lcdHost.shadowRoot.querySelectorAll(
                        '.lcd-meta__secondary .lcd-meta-line__fragment[tabindex="0"]'
                    );
                    return frags.length ? Array.from(frags).map(f => f.textContent).join('').trim() : null;
                } catch (e) { return null; }
            """)

            if title:
                song_info["title"] = title
                self.logger.debug(f"Found title via shadow DOM: {title}")

            if artist_raw:
                # Format is "Artist — Album", keep only the artist part
                parts = artist_raw.split(" — ")
                song_info["artist"] = parts[0].strip()
                self.logger.debug(f"Found artist via shadow DOM: {song_info['artist']}")

            # FALLBACK: If shadow DOM query failed, try the regular track list
            if not song_info["title"]:
                self.logger.info("Shadow DOM title not found, falling back to track list...")
                track_list_items = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='track-list-item']")
                for item in track_list_items[:5]:
                    try:
                        title_el = item.find_element(By.CSS_SELECTOR, "[data-testid='track-title']")
                        if title_el and title_el.text.strip():
                            song_info["title"] = title_el.text.strip()
                            self.logger.debug(f"Found title from track list: {song_info['title']}")
                            try:
                                artist_el = item.find_element(By.CSS_SELECTOR,
                                                              "[data-testid='track-title-by-line'] a")
                                if artist_el:
                                    song_info["artist"] = artist_el.text.strip()
                            except Exception:
                                pass
                            break
                    except Exception:
                        continue

            # Try to get track number from the track list
            track_list_selector = nav_config.get("track_list_item_selector")
            if track_list_selector:
                track_list_items = self.driver.find_elements(By.CSS_SELECTOR, track_list_selector)
                if track_list_items:
                    try:
                        track_number = track_list_items[0].get_attribute("data-row")
                        if track_number:
                            song_info["track_number"] = track_number
                    except Exception:
                        pass

            if not song_info["title"]:
                self.logger.debug("Could not detect song title from shadow DOM or track list")

        except Exception as e:
            self.logger.warning(f"Error detecting song info: {e}")

        return song_info

    def _is_lcd_showing_play_button(self) -> bool:
        """Return True when LCD shows Play (and not Pause), indicating playback stopped.

        Apple Music controls can be nested in open shadow roots, so this uses a
        deep query in JS that walks shadow DOM boundaries.
        """
        try:
            state = self.driver.execute_script("""
                function looksActive(el) {
                    if (!el) return false;
                    // Apple Music often toggles state via aria-hidden while keeping both
                    // controls mounted, so prefer accessibility state over geometry.
                    if (el.getAttribute('aria-hidden') === 'true') return false;
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;

                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    return true;
                }

                function deepQueryAll(root, selector, out) {
                    if (!root) return;
                    out.push(...root.querySelectorAll(selector));
                    const all = root.querySelectorAll('*');
                    for (const node of all) {
                        if (node.shadowRoot) {
                            deepQueryAll(node.shadowRoot, selector, out);
                        }
                    }
                }

                const playBtns = [];
                const pauseBtns = [];
                deepQueryAll(document, 'button.playback-play__play', playBtns);
                deepQueryAll(document, 'button.playback-play__pause', pauseBtns);

                const playActive = playBtns.some(looksActive);
                const pauseActive = pauseBtns.some(looksActive);

                return {
                    playActive,
                    pauseActive,
                    playCount: playBtns.length,
                    pauseCount: pauseBtns.length
                };
            """)

            play_active = bool(state and state.get("playActive"))
            pause_active = bool(state and state.get("pauseActive"))

            self.logger.debug(
                "LCD controls state: play_active=%s pause_active=%s play_count=%s pause_count=%s",
                play_active,
                pause_active,
                state.get("playCount") if state else None,
                state.get("pauseCount") if state else None,
            )

            return play_active and not pause_active
        except Exception as e:
            self.logger.debug(f"Could not read LCD play/pause state: {e}")
            return False

    def _get_monitor_device(self) -> int | None:
        """Configure PulseAudio to capture from the sink monitor (system audio loopback).

        On Linux, each PulseAudio sink has a .monitor source that captures whatever
        audio is playing to that output.  We:
          1. Find the default sink from `pactl info`
          2. Confirm the {sink}.monitor source exists via `pactl list sources short`
          3. Set PULSE_SOURCE so the PulseAudio PortAudio backend uses it
          4. Return the index of sounddevice's 'pulse' device

        Returns the sounddevice device index to pass to sd.InputStream, or None
        to use the system default (which may be a microphone).
        """
        if not sys.platform.startswith("linux"):
            return None

        try:
            # Step 1 – get default sink name
            info_result = subprocess.run(
                ["pactl", "info"], capture_output=True, text=True, timeout=5
            )
            default_sink = ""
            for line in info_result.stdout.splitlines():
                if line.startswith("Default Sink:"):
                    default_sink = line.split(":", 1)[1].strip()
                    break

            if not default_sink:
                self.logger.warning("Could not determine default PulseAudio sink.")
                return None

            monitor_name = f"{default_sink}.monitor"

            # Step 2 – confirm the monitor source exists
            sources_result = subprocess.run(
                ["pactl", "list", "sources", "short"],
                capture_output=True, text=True, timeout=5
            )
            monitor_exists = any(
                monitor_name in line
                for line in sources_result.stdout.splitlines()
            )

            if not monitor_exists:
                # Try any .monitor source as fallback
                for line in sources_result.stdout.splitlines():
                    if ".monitor" in line:
                        monitor_name = line.split()[1]
                        monitor_exists = True
                        break

            if not monitor_exists:
                self.logger.warning(
                    "No monitor source found in PulseAudio.  "
                    "System audio cannot be captured."
                )
                return None

            # Step 3 – point the PA PortAudio backend at the monitor
            os.environ["PULSE_SOURCE"] = monitor_name
            self.logger.info(f"Set PULSE_SOURCE={monitor_name}")

            # Step 4 – find the 'pulse' sounddevice
            devices = sd.query_devices()
            for i, dev in enumerate(devices):
                if dev["name"].lower() == "pulse" and dev["max_input_channels"] > 0:
                    self.logger.info(f"Using sounddevice 'pulse' device at index {i}")
                    return i

            self.logger.warning("sounddevice 'pulse' device not found; falling back to default input.")
            return None

        except Exception as e:
            self.logger.warning(f"Could not configure monitor device: {e}")
            return None

    def _start_recording(self):
        """Start recording system audio."""
        recording_config = self.config.get("recording", {})
        sample_rate = recording_config.get("sample_rate", 44100)
        channels = recording_config.get("channels", 2)
        chunk_duration = recording_config.get("chunk_duration", 0.1)

        self.audio_buffer = []
        self.recording_start_time = time.time()

        # Start audio stream callback
        self.is_recording = True

        def audio_callback(indata, frames, time_t, status):
            # sounddevice passes CallbackFlags; it's truthy only when flags are set
            # (overflow/underflow/etc.). Avoid int comparisons which can misfire.
            if status:
                self.logger.warning("Audio callback status: %s", status)

            if not self.is_recording:
                return

            # indata is already int16 (dtype set on the stream); just copy the bytes
            with self._audio_lock:
                self.audio_buffer.append(indata.tobytes())

        # Open audio stream
        try:
            # On Linux, capture from the PulseAudio/PipeWire monitor source
            # so we get the system audio output rather than the microphone.
            monitor_device = self._get_monitor_device()

            self.audio_stream = sd.InputStream(
                device=monitor_device,  # None = default input on non-Linux
                samplerate=sample_rate,
                channels=channels,
                dtype='int16',  # request int16 directly; default float32 causes noise
                callback=audio_callback,
                blocksize=int(sample_rate * chunk_duration)
            )
            self.audio_stream.start()
            self.logger.info(f"Audio recording started: {sample_rate}Hz, {channels}ch")

        except Exception as e:
            self.logger.error(f"Failed to start audio recording: {e}")
            raise

    def _stop_recording(self) -> bytes:
        """Stop recording and return raw audio data."""
        self.is_recording = False

        if self.audio_stream:
            self.audio_stream.stop()
            self.audio_stream.close()
            self.audio_stream = None

        # Concatenate all audio chunks
        if self.audio_buffer:
            audio_data = b''.join(self.audio_buffer)
            self.audio_buffer = []
            return audio_data
        return b''

    def _save_audio(self, audio_data: bytes, song_info: dict):
        """Save recorded audio as MP3 file."""
        output_config = self.config.get("output", {})
        output_dir = Path(output_config.get("directory", "./recordings"))
        naming_convention = output_config.get("naming_convention", "{artist}_{title}")
        bitrate = output_config.get("bitrate", "192k")
        recording_config = self.config.get("recording", {})
        sample_rate = recording_config.get("sample_rate", 44100)
        channels = recording_config.get("channels", 2)

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename
        track_num = song_info.get("track_number", "0")
        artist = song_info.get("artist", "Unknown Artist")
        title = song_info.get("title", "Unknown Title")

        # Sanitize filename
        def sanitize(text):
            return "".join(c for c in text if c.isalnum() or c in ' -_').strip()

        filename = naming_convention.format(
            track_number=sanitize(str(track_num)),
            artist=sanitize(artist),
            title=sanitize(title),
            timestamp=datetime.now().strftime("%Y%m%d_%H%M%S")
        )

        # Add extension and ensure uniqueness
        extension = ".mp3"
        filepath = output_dir / f"{filename}{extension}"

        # Handle duplicate filenames
        counter = 1
        while filepath.exists():
            filepath = output_dir / f"{filename}_{counter}{extension}"
            counter += 1

        # Save audio data
        recording_duration = time.time() - self.recording_start_time if self.recording_start_time else 0
        self.logger.info(f"Saving audio to: {filepath} (duration: {recording_duration:.1f}s)")

        # Convert to MP3 using ffmpeg
        try:
            # Prepare ffmpeg process
            ffmpeg_input = ffmpeg.input('pipe:', format='s16le',
                                        ar=sample_rate, ac=channels,
                                        channel_layout='stereo')
            ffmpeg_output = ffmpeg_input.output('pipe:', format='mp3',
                                                b=bitrate, ab=bitrate)
            out, _ = ffmpeg_output.run(
                input=audio_data,
                capture_stdout=True,
                capture_stderr=True
            )

            # Write MP3 file
            with open(filepath, 'wb') as f:
                f.write(out)

            # Add metadata
            self._add_metadata(filepath, song_info)

        except Exception as e:
            self.logger.error(f"Error encoding MP3 with ffmpeg: {e}")
            # Fallback to WAV
            self._save_wav(filepath.with_suffix('.wav'), audio_data, sample_rate, channels)

        self.recorded_songs.append({
            "filepath": str(filepath),
            "song_info": song_info,
            "duration": recording_duration,
            "timestamp": datetime.now().isoformat()
        })

        self.logger.info(f"Saved: {filepath}")

    def _save_wav(self, filepath: Path, audio_data: bytes, sample_rate: int, channels: int):
        """Save audio as WAV file (fallback)."""
        try:
            import struct

            with open(filepath, 'wb') as f:
                # WAV header
                f.write(b'RIFF')
                f.write(struct.pack('<I', 36 + len(audio_data)))
                f.write(b'WAVE')
                f.write(b'fmt ')
                f.write(struct.pack('<I', 16))
                f.write(struct.pack('<H', 1))  # PCM
                f.write(struct.pack('<H', channels))
                f.write(struct.pack('<I', sample_rate))
                f.write(struct.pack('<I', sample_rate * channels * 2))
                f.write(struct.pack('<H', channels * 2))
                f.write(struct.pack('<H', 16))
                f.write(b'data')
                f.write(struct.pack('<I', len(audio_data)))
                f.write(audio_data)

            self.logger.info(f"Saved WAV: {filepath}")

        except Exception as e:
            self.logger.error(f"Error saving WAV: {e}")

    def _add_metadata(self, filepath: Path, song_info: dict):
        """Add metadata to MP3 file."""
        try:
            title = song_info.get("title", "")
            artist = song_info.get("artist", "")

            # Load existing ID3 tags if present, otherwise create fresh ones
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()

            if title:
                tags["TIT2"] = TIT2(encoding=3, text=title)
            if artist:
                tags["TPE1"] = TPE1(encoding=3, text=artist)

            tags.save(filepath)
            self.logger.debug(f"Added metadata to {filepath}")

        except Exception as e:
            self.logger.warning(f"Could not add metadata: {e}")

    def _handle_song_start(self, song_info: dict):
        """Handle song start event."""
        self.song_count += 1
        self.current_recording = dict(song_info)
        self.logger.info(f"=== Song #{self.song_count} ===")
        self.logger.info(f"Title: {song_info.get('title', 'Unknown')}")
        self.logger.info(f"Artist: {song_info.get('artist', 'Unknown')}")

        if self.on_song_start:
            self.on_song_start(self.song_count, song_info)

    def _handle_song_end(self):
        """Handle song end event."""
        if self.recording_start_time:
            duration = time.time() - self.recording_start_time
            self.logger.info(f"Song recording ended. Duration: {duration:.1f}s")

            # Use the song that started this recording so title-change detection
            # does not save the file with the next track's metadata.
            song_info = self.current_recording or {}

            # Save the recording
            audio_data = self._stop_recording()
            if audio_data:
                self._save_audio(audio_data, song_info)

        self.recording_start_time = None
        self.current_recording = None

    def _discard_current_recording(self, reason: str = ""):
        """Stop and discard the in-progress recording without saving a file."""
        if self.recording_start_time:
            duration = time.time() - self.recording_start_time
            suffix = f" ({reason})" if reason else ""
            self.logger.info(f"Discarding current recording{suffix}. Duration: {duration:.1f}s")

        # Stop stream and clear audio buffer, but do not write output.
        self._stop_recording()
        self.recording_start_time = None
        self.current_recording = None

    def _check_max_songs(self) -> bool:
        """Check if we've reached the maximum number of songs."""
        max_songs = self.config.get("output", {}).get("max_songs", -1)
        if max_songs > 0 and self.song_count >= max_songs:
            self.logger.info(f"Reached max songs limit: {max_songs}")
            return True
        return False

    def _process_playlist(self):
        """Process the playlist and record songs."""
        self.logger.info("Starting playlist processing...")

        # Wait for initial page to load
        time.sleep(3)

        # Check if a song is already playing by looking for play button
        # If play button is visible, nothing is playing - click it to start
        play_buttons = self.driver.find_elements(By.CSS_SELECTOR, "[data-testid='play-button']")
        if play_buttons:
            self.logger.info("No song is currently playing. Clicking play button to start the playlist...")
            try:
                # Click the first visible play button (usually a global play button for the playlist)
                play_button = play_buttons[0]
                self.driver.execute_script("arguments[0].click();", play_button)
                self.logger.info("Play button clicked. Waiting for song to start...")
                time.sleep(2)
            except Exception as e:
                self.logger.warning(f"Failed to click play button: {e}")

        # Detect first song
        song_info = {}
        while not song_info.get("title"):
            song_info = self._get_current_song_info()
            if song_info["title"]:
                self.logger.info(f"Detected initial song: {song_info['title']} by {song_info['artist']}")
                self._handle_song_start(song_info)
                self._start_recording()
            else:
                self.logger.warning("Could not detect initial song. Waiting and trying again...")
                time.sleep(2)

        # Click play btn after recording starts so we don't miss audio
        try:
            self.logger.info("Attempting to find and click playback-play__pause button in shadow DOM...")
            playback_button = self._click_play_btn()

            if playback_button is None:
                raise Exception("Could not find playback-play__pause button")
        except Exception as e:
            self.logger.warning(f"Could not click playback-play__pause button via shadow DOM: {e}")

        # Main loop
        while not self.stop_event.is_set():
            # Check max songs
            if self._check_max_songs():
                break

            # Wait for recording to complete
            time.sleep(0.05)

            # Exit cleanly when playback reaches playlist end and LCD returns to Play.
            if self._is_lcd_showing_play_button():
                self._play_button_visible_streak += 1
            else:
                self._play_button_visible_streak = 0

            if self._play_button_visible_streak >= 1000:
                self.logger.info("Detected LCD Play button state at playlist end. Stopping recorder.")
                self.stop()
                continue

            # End the current recording when Apple Music advances to a new title.
            if self.is_recording and self.current_recording:
                current_song = self._get_current_song_info()
                current_title = (current_song.get("title") or "").strip()
                recording_title = (self.current_recording.get("title") or "").strip()

                # Only log if titles are found (avoid spam)
                if current_title and recording_title:
                    self.logger.debug(
                        f"Current: '{current_title}' | Recording: '{recording_title}'"
                    )

                    if current_title != recording_title:
                        self.logger.info(
                            f"Song change detected: '{recording_title}' -> '{current_title}'"
                        )
                        # Click play to pause new song while we save
                        self._click_play_btn()

                        self._handle_song_end()
                        self._handle_song_start(current_song)

                        # Reset song to start because it can take a second for the title to load
                        self._reset_progress_bar()
                        self._click_play_btn()
                        time.sleep(0.05)
                        self._start_recording()
                        continue
        # This else runs when stop is called
        else:
            self.logger.info("Playlist processing stopped")
            self._discard_current_recording("stopped")

    def _click_play_btn(self) -> WebElement | None:
        """
        Clicks and then returns the play/pause button element within the Apple Music player's shadow DOM structure.

        Returns:
            WebElement: The play/pause button element if found, otherwise None.
        """
        # 1. Find the first shadow host
        player_host = self.driver.find_element(By.CSS_SELECTOR, "amp-chrome-player")
        player_shadow = player_host.shadow_root

        # 2. Find the second shadow host inside the first shadow root
        controls_host = player_shadow.find_element(By.CSS_SELECTOR, "apple-music-playback-controls")
        controls_shadow = controls_host.shadow_root

        # 3. Find the play/pause button in the second shadow root
        playback_button_pause = controls_shadow.find_element(By.CSS_SELECTOR, ".playback-play__pause")
        playback_button_play = controls_shadow.find_element(By.CSS_SELECTOR, ".playback-play__play")

        if not (playback_button_pause or playback_button_play):
            return None
        else:
            active_btn = playback_button_pause if playback_button_pause else playback_button_play

        self.logger.info(f"Found playback-play__pause button. {active_btn} Clicking...")
        self.driver.execute_script("arguments[0].click();", active_btn)

        return active_btn

    def _reset_progress_bar(self) -> None:
        """
        Sets the progress bar to 0%.
        """
        # bar is in lcd shadow root
        player_host = self.driver.find_element(By.CLASS_NAME, "lcd")
        player_shadow = player_host.shadow_root

        # Main clickable progress bar
        progress_bar: WebElement = player_shadow.find_element(By.CSS_SELECTOR, "#playback-progress")

        # Use navigation as anchor for left side of screen
        navi = self.driver.find_element(By.CSS_SELECTOR, "#navigation")

        # Reset progress bar by clicking the middle and then dragging to navi
        if progress_bar:
            self.logger.info(f"Found progress bar. Setting to 0%...")
            ActionChains(self.driver).move_to_element(progress_bar).click_and_hold(progress_bar).move_to_element(
                navi).pause(0.3).release().perform()
            self.logger.info(f"Set to 0%")
        else:
            self.logger.warning("Could not find progress bar. Skipping reset.")

    def _cleanup(self):
        """Clean up resources."""
        self.logger.info("Cleaning up...")

        if self.audio_stream:
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except Exception:
                pass

        if self.driver:
            try:
                # In some end-of-playlist states ChromeDriver can hang on quit().
                quit_thread = Thread(target=self.driver.quit, daemon=True)
                quit_thread.start()
                quit_thread.join(timeout=8)
                if quit_thread.is_alive():
                    self.logger.warning("Browser quit timed out; continuing shutdown.")
            except Exception:
                pass

        self.logger.info("Cleanup complete.")

    def run(self):
        """Main entry point for the recorder."""
        try:
            self.logger.info("=" * 50)
            self.logger.info("Apple Music Recorder Starting")
            self.logger.info("=" * 50)

            # Setup browser
            self._setup_browser()

            # Navigate to Apple Music and check login status
            login_success = self._navigate_to_music()

            if not login_success:  # TODO this login check doesnt work
                # Try one more time with extended wait
                self.logger.info("Attempting extended login wait...")
                self.logger.info("Please login to Apple Music in the browser window.")
                self.logger.info("Waiting 120 seconds for login...")
                time.sleep(120)

                # Check login status again
                login_success = self._check_login_status()

            if not login_success:
                self.logger.error(
                    "Failed to authenticate with Apple Music. Please run the script again after logging in manually.")
                return

            self.logger.info("Authenticated successfully. Starting recording process...")

            # Start recording
            self._process_playlist()

        except KeyboardInterrupt:
            self.logger.info("Interrupted by user.")
        except Exception as e:
            self.logger.error(f"Error: {e}", exc_info=True)
            if self.on_error:
                self.on_error(e)
        finally:
            self._cleanup()

        # Print summary
        self._print_summary()

    def stop(self):
        """Stop the recorder."""
        self.logger.info("Stopping recorder...")
        self.stop_event.set()
        self.is_recording = False

    def _print_summary(self):
        """Print recording summary."""
        self.logger.info("=" * 50)
        self.logger.info("Recording Summary")
        self.logger.info("=" * 50)
        self.logger.info(f"Total songs recorded: {len(self.recorded_songs)}")

        total_duration = sum(s["duration"] for s in self.recorded_songs)
        self.logger.info(f"Total recording time: {total_duration:.1f}s")

        if self.recorded_songs:
            self.logger.info("Recorded songs:")
            for i, song in enumerate(self.recorded_songs, 1):
                self.logger.info(
                    f"  {i}. {song['song_info'].get('title', 'Unknown')} - "
                    f"{song['song_info'].get('artist', 'Unknown')} "
                    f"({song['duration']:.1f}s)"
                )

        self.logger.info(f"Output directory: {self.config.get('output', {}).get('directory', './recordings')}")
        self.logger.info("=" * 50)


def main():
    """Main entry point for command line usage."""
    parser = argparse.ArgumentParser(
        description="Apple Music Recorder - Record songs from Apple Music",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python apple_music_recorder.py                    # Use default config
  python apple_music_recorder.py --config my.json   # Use custom config
  python apple_music_recorder.py --playlist URL     # Override playlist URL
  python apple_music_recorder.py --max-songs 10     # Record max 10 songs
  python apple_music_recorder.py --headless         # Run in headless mode
  python apple_music_recorder.py --output ./myrecs  # Custom output directory
        """
    )

    parser.add_argument("--config", "-c", default="config.json",
                        help="Path to configuration file (default: config.json)")
    parser.add_argument("--playlist", "-p", default=None,
                        help="Apple Music playlist URL to record")
    parser.add_argument("--max-songs", "-m", type=int, default=-1,
                        help="Maximum number of songs to record (default: unlimited)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output directory for recordings")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser in headless mode")
    parser.add_argument("--browser", "-b", choices=["chrome", "firefox"],
                        help="Browser to use (default: from config)")
    parser.add_argument("--sample-rate", "-r", type=int,
                        help="Audio sample rate (default: from config)")
    parser.add_argument("--bitrate", "-R", default=None,
                        help="MP3 bitrate (default: from config)")
    parser.add_argument("--log-level", "-l", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        default=None, help="Logging level")
    parser.add_argument("--dry-run", action="store_true",
                        help="Setup and exit without recording")

    args = parser.parse_args()

    # Create recorder
    recorder = AppleMusicRecorder(args.config)

    # Apply command line overrides
    if args.playlist:
        recorder.config["playlist_url"] = args.playlist
    if args.max_songs > 0:
        recorder.config["output"]["max_songs"] = args.max_songs
    if args.output:
        recorder.config["output"]["directory"] = args.output
    if args.headless:
        recorder.config["browser"]["headless"] = True
    if args.browser:
        recorder.config["browser"]["type"] = args.browser
    if args.sample_rate:
        recorder.config["recording"]["sample_rate"] = args.sample_rate
    if args.bitrate:
        recorder.config["output"]["bitrate"] = args.bitrate
    if args.log_level:
        recorder.config["logging"]["level"] = args.log_level

    if args.dry_run:
        print("Dry run complete. Configuration:")
        print(json.dumps(recorder.config, indent=2))
        return

    # Run the recorder
    recorder.run()


if __name__ == "__main__":
    main()
