"""
Audio Backend Abstraction for RoIP Audio Simulator (audio_backend.py)
Provides both live hardware audio (via optional sounddevice) and a zero-dependency
synthetic audio generator / WAV recorder using Python's standard library.
"""

import math
import struct
import wave
import time
import queue
import threading

SAMPLE_RATE = 16000          # 16 kHz
SAMPLE_WIDTH = 2             # 16-bit PCM (2 bytes per sample)
CHANNELS = 1                 # Mono
FRAME_DURATION_MS = 20       # 20ms audio frame
SAMPLES_PER_FRAME = 320      # 16000 * 0.02 = 320 samples
BYTES_PER_FRAME = 640        # 320 samples * 2 bytes = 640 bytes


_STRUCT_320H = struct.Struct("<320h")
SILENCE_FRAME = b"\x00" * BYTES_PER_FRAME


def calculate_rms_db(pcm_data: bytes) -> float:
    """
    Computes RMS level in dBFS for 16-bit signed PCM data.
    Returns value between -96.0 dBFS and 0.0 dBFS.
    """
    if not pcm_data or len(pcm_data) < 2 or pcm_data == SILENCE_FRAME:
        return -96.0

    count = len(pcm_data) // 2
    try:
        if count == SAMPLES_PER_FRAME:
            samples = _STRUCT_320H.unpack_from(pcm_data, 0)
        else:
            samples = struct.unpack(f"<{count}h", pcm_data[:count * 2])
    except struct.error:
        return -96.0

    sum_squares = sum(s * s for s in samples)
    if sum_squares <= 0:
        return -96.0

    mean_square = sum_squares / count
    rms = math.sqrt(mean_square)
    # Full scale peak is 32767, RMS is 32767 / sqrt(2) = 23170.4
    db = 20 * math.log10(rms / 32767.0)
    return max(-96.0, min(0.0, db))


def format_vu_meter(db: float, width: int = 15) -> str:
    """
    Generates an ASCII VU meter representation.
    e.g. [||||||.........] -18.2 dB
    """
    clamped_db = max(-60.0, min(0.0, db))
    fraction = (clamped_db + 60.0) / 60.0
    filled = int(fraction * width)
    bar = "|" * filled + "." * (width - filled)
    return f"[{bar}] {db:5.1f} dB"


def generate_tone_frames(freq: float = 1200.0, duration_ms: int = 80, amplitude: int = 12000) -> list:
    """
    Generates a list of 20ms PCM audio frames (each 640 bytes) containing a sine tone.
    Used for radio roger beeps, courtesy tones, and channel busy alert tones.
    """
    total_samples = int(SAMPLE_RATE * (duration_ms / 1000.0))
    frames = []
    samples = []
    for i in range(total_samples):
        t = i / SAMPLE_RATE
        val = int(amplitude * math.sin(2.0 * math.pi * freq * t))
        samples.append(max(-32768, min(32767, val)))
        if len(samples) == SAMPLES_PER_FRAME:
            frames.append(_STRUCT_320H.pack(*samples))
            samples = []

    if samples:
        samples += [0] * (SAMPLES_PER_FRAME - len(samples))
        frames.append(_STRUCT_320H.pack(*samples))

    return frames


# International Morse Code mapping for Part 97 station identification
MORSE_CODE_DICT = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    "/": "-..-.", "?": "..--..", ".": ".-.-.-", ",": "--..--", "-": "-....-",
    "=": "-...-", ":": "---...", ";": "-.-.-.", "(": "-.--.", ")": "-.--.-",
}


def _samples_to_frames(samples: list) -> list:
    """Slices a flat list of 16-bit PCM integer samples into 20ms (640-byte) frames."""
    frames = []
    for i in range(0, len(samples), SAMPLES_PER_FRAME):
        chunk = samples[i : i + SAMPLES_PER_FRAME]
        if len(chunk) < SAMPLES_PER_FRAME:
            chunk += [0] * (SAMPLES_PER_FRAME - len(chunk))
        frames.append(_STRUCT_320H.pack(*chunk))
    return frames


def _generate_shaped_tone_samples(
    freq: float, duration_ms: float, amplitude: int = 12000, sample_rate: int = SAMPLE_RATE
) -> list:
    """Generates sine wave samples with 5ms raised-cosine attack and decay to prevent key clicks."""
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    if num_samples <= 0:
        return []
    edge_samples = min(int(sample_rate * 0.005), num_samples // 2)
    samples = []
    for i in range(num_samples):
        if edge_samples > 0 and i < edge_samples:
            env = 0.5 * (1.0 - math.cos(math.pi * i / edge_samples))
        elif edge_samples > 0 and i >= (num_samples - edge_samples):
            env = 0.5 * (1.0 + math.cos(math.pi * (i - (num_samples - edge_samples)) / edge_samples))
        else:
            env = 1.0
        t = i / sample_rate
        val = int(amplitude * env * math.sin(2.0 * math.pi * freq * t))
        samples.append(max(-32768, min(32767, val)))
    return samples


def generate_morse_frames(
    text: str, wpm: int = 25, freq: float = 800.0, amplitude: int = 14000
) -> list:
    """
    Generates a list of 20ms PCM audio frames (each 640 bytes) containing International Morse Code.
    Follows standard PARIS timing (Dit = 1200 / wpm ms).
    Smooth 5ms raised-cosine envelope on key-down and key-up prevents key clicks.
    """
    dit_ms = 1200.0 / max(5, wpm)
    dah_ms = 3.0 * dit_ms
    intra_element_gap_samples = [0] * int(SAMPLE_RATE * (dit_ms / 1000.0))
    inter_char_gap_samples = [0] * int(SAMPLE_RATE * ((3.0 * dit_ms) / 1000.0))
    inter_word_gap_samples = [0] * int(SAMPLE_RATE * ((7.0 * dit_ms) / 1000.0))

    dit_samples = _generate_shaped_tone_samples(freq, dit_ms, amplitude=amplitude)
    dah_samples = _generate_shaped_tone_samples(freq, dah_ms, amplitude=amplitude)

    all_samples = []
    # 20ms lead-in silence for clean transmitter keyup
    all_samples.extend([0] * SAMPLES_PER_FRAME)

    words = text.upper().strip().split()
    for w_idx, word in enumerate(words):
        if w_idx > 0:
            all_samples.extend(inter_word_gap_samples)

        for c_idx, char in enumerate(word):
            if c_idx > 0:
                all_samples.extend(inter_char_gap_samples)

            morse_pattern = MORSE_CODE_DICT.get(char, "")
            for e_idx, element in enumerate(morse_pattern):
                if e_idx > 0:
                    all_samples.extend(intra_element_gap_samples)

                if element == ".":
                    all_samples.extend(dit_samples)
                elif element == "-":
                    all_samples.extend(dah_samples)

    # 40ms trailing silence before transmitter unkey
    all_samples.extend([0] * (SAMPLES_PER_FRAME * 2))
    return _samples_to_frames(all_samples)


# DTMF Dual-Tone Frequencies (ITU-T Q.23 standard)
DTMF_FREQUENCIES = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633),
}


def generate_dtmf_frames(
    digit: str, duration_ms: int = 100, gap_ms: int = 40, amplitude: int = 12000
) -> list:
    """
    Generates a list of 20ms PCM audio frames containing standard ITU-T DTMF dual tones.
    Used for Part 97 auxiliary station telecommand and control signaling.
    """
    digit_char = str(digit).upper()
    if digit_char not in DTMF_FREQUENCIES:
        return []

    f1, f2 = DTMF_FREQUENCIES[digit_char]
    total_samples = int(SAMPLE_RATE * (duration_ms / 1000.0))
    edge_samples = min(int(SAMPLE_RATE * 0.005), total_samples // 2)

    samples = []
    for i in range(total_samples):
        if edge_samples > 0 and i < edge_samples:
            env = 0.5 * (1.0 - math.cos(math.pi * i / edge_samples))
        elif edge_samples > 0 and i >= (total_samples - edge_samples):
            env = 0.5 * (1.0 + math.cos(math.pi * (i - (total_samples - edge_samples)) / edge_samples))
        else:
            env = 1.0

        t = i / SAMPLE_RATE
        val = int(0.5 * amplitude * env * (math.sin(2.0 * math.pi * f1 * t) + math.sin(2.0 * math.pi * f2 * t)))
        samples.append(max(-32768, min(32767, val)))

    if gap_ms > 0:
        samples.extend([0] * int(SAMPLE_RATE * (gap_ms / 1000.0)))

    return _samples_to_frames(samples)


def generate_courtesy_tone_frames(style: str = "chime", amplitude: int = 12000) -> list:
    """
    Generates authentic Part 97 simplex/repeater courtesy tones and station alert beeps.
    Supported styles:
    - 'chime': Dual-tone auxiliary link chime (880 Hz for 50ms, then 1046 Hz for 65ms)
    - 'single': Classic 1200 Hz 80ms beep
    - 'quindar': NASA Quindar tone (2475 Hz for 80ms)
    - 'boop': Low 400 Hz 140ms warning tone (used for Time-Out Timer cutoff and lockout)
    """
    style_lower = (style or "chime").lower()
    if style_lower == "none":
        return []
    elif style_lower == "single":
        return generate_tone_frames(freq=1200.0, duration_ms=80, amplitude=amplitude)
    elif style_lower == "quindar":
        return generate_tone_frames(freq=2475.0, duration_ms=80, amplitude=amplitude)
    elif style_lower == "boop":
        return generate_tone_frames(freq=400.0, duration_ms=140, amplitude=amplitude)
    else:  # 'chime' (auxiliary link dual tone)
        samples = []
        samples.extend(_generate_shaped_tone_samples(880.0, duration_ms=50, amplitude=amplitude))
        samples.extend([0] * int(SAMPLE_RATE * 0.015))  # 15ms gap
        samples.extend(_generate_shaped_tone_samples(1046.5, duration_ms=65, amplitude=amplitude))
        samples.extend([0] * int(SAMPLE_RATE * 0.020))  # 20ms tail
        return _samples_to_frames(samples)



class AudioBackend:
    """Abstract base class for audio backends."""

    def start(self):
        """Start audio streams or generators."""
        raise NotImplementedError

    def stop(self):
        """Stop audio streams or generators and clean up resources."""
        raise NotImplementedError

    def flush_input(self):
        """Flushes any buffered input frames before transmission starts."""
        pass

    def has_buffered_input(self) -> bool:
        """Returns True if there are unread input frames in the capture queue."""
        return False

    def read_frame(self) -> bytes:
        """
        Reads one 20ms audio frame (640 bytes, 16 kHz 16-bit signed mono PCM).
        Must block until available or return 640 bytes.
        """
        raise NotImplementedError

    def write_frame(self, pcm_data: bytes):
        """
        Outputs or buffers one 20ms audio frame for playback / recording.
        """
        raise NotImplementedError


class SyntheticAudioBackend(AudioBackend):
    """
    Standard-library-only audio backend.
    Generates 1 kHz test tones, roger beeps, or silence without requiring external C libraries.
    Optionally records received audio to a WAV file and/or plays from a WAV file.
    """

    def __init__(self, tone_freq: float = 1000.0, record_path: str = None, wav_play_path: str = None):
        self.tone_freq = tone_freq
        self.record_path = record_path
        self.wav_play_path = wav_play_path

        self._running = False
        self._sample_index = 0
        self._wav_in = None
        self._wav_out = None
        self._out_lock = threading.Lock()

        # Tone modes: 'tone', 'silence', 'roger_beep'
        self.tone_mode = "tone"

    def flush_input(self):
        self._sample_index = 0

    def start(self):
        self._running = True
        self._sample_index = 0

        if self.wav_play_path:
            try:
                self._wav_in = wave.open(self.wav_play_path, "rb")
            except Exception as e:
                print(f"[!] Warning: Could not open play WAV file '{self.wav_play_path}': {e}")
                self._wav_in = None

        if self.record_path:
            try:
                self._wav_out = wave.open(self.record_path, "wb")
                self._wav_out.setnchannels(CHANNELS)
                self._wav_out.setsampwidth(SAMPLE_WIDTH)
                self._wav_out.setframerate(SAMPLE_RATE)
            except Exception as e:
                print(f"[!] Warning: Could not create record WAV file '{self.record_path}': {e}")
                self._wav_out = None

    def stop(self):
        self._running = False
        with self._out_lock:
            if self._wav_out:
                try:
                    self._wav_out.close()
                except Exception:
                    pass
                self._wav_out = None

        if self._wav_in:
            try:
                self._wav_in.close()
            except Exception:
                pass
            self._wav_in = None

    def read_frame(self) -> bytes:
        """Generates exactly 640 bytes of 16-bit PCM."""
        if not self._running:
            return b"\x00" * BYTES_PER_FRAME

        # If WAV playback is active
        if self._wav_in:
            data = self._wav_in.readframes(SAMPLES_PER_FRAME)
            if len(data) == BYTES_PER_FRAME:
                return data
            # Loop playback
            self._wav_in.rewind()
            data = self._wav_in.readframes(SAMPLES_PER_FRAME)
            if len(data) == BYTES_PER_FRAME:
                return data

        if self.tone_mode == "silence":
            return b"\x00" * BYTES_PER_FRAME

        # Generate sine wave tone
        samples = []
        amplitude = 16000  # ~ -6 dBFS
        freq = self.tone_freq

        for _ in range(SAMPLES_PER_FRAME):
            t = self._sample_index / SAMPLE_RATE
            val = int(amplitude * math.sin(2.0 * math.pi * freq * t))
            samples.append(max(-32768, min(32767, val)))
            self._sample_index += 1

        # Keep sample_index bounded to avoid precision degradation
        if self._sample_index >= SAMPLE_RATE * 60:
            self._sample_index = 0

        return struct.pack(f"<{SAMPLES_PER_FRAME}h", *samples)

    def write_frame(self, pcm_data: bytes):
        """Writes incoming audio frame to disk if recording is enabled."""
        if not self._running or not pcm_data:
            return

        with self._out_lock:
            if self._wav_out:
                try:
                    self._wav_out.writeframes(pcm_data)
                except Exception:
                    pass


class SounddeviceBackend(AudioBackend):
    """
    Live hardware audio backend using the sounddevice library.
    Captures live microphone input and streams output to speakers with ultra-low latency.
    """

    def __init__(self, record_path: str = None):
        self.record_path = record_path
        self._running = False
        self._stream = None
        # Keep queue depths minimal (under 60-80ms) to eliminate delay and latency buildup
        self._in_queue = queue.Queue(maxsize=3)
        self._out_queue = queue.Queue(maxsize=3)
        self._wav_out = None
        self._out_lock = threading.Lock()

    def has_buffered_input(self) -> bool:
        """Returns True if there are captured mic frames awaiting transmission."""
        return not self._in_queue.empty()

    def flush_input(self):
        """Discards any stale audio buffered prior to PTT keying so speech starts instantly."""
        while not self._in_queue.empty():
            try:
                self._in_queue.get_nowait()
            except Exception:
                break

    def start(self):
        import sounddevice as sd

        self._running = True

        if self.record_path:
            try:
                self._wav_out = wave.open(self.record_path, "wb")
                self._wav_out.setnchannels(CHANNELS)
                self._wav_out.setsampwidth(SAMPLE_WIDTH)
                self._wav_out.setframerate(SAMPLE_RATE)
            except Exception as e:
                print(f"[!] Warning: Could not create record WAV file '{self.record_path}': {e}")
                self._wav_out = None

        def audio_callback(indata, outdata, frames, time_info, status):
            # Input capture: always keep the freshest frames in queue
            raw_in = bytes(indata)
            try:
                self._in_queue.put_nowait(raw_in)
            except queue.Full:
                try:
                    self._in_queue.get_nowait()
                    self._in_queue.put_nowait(raw_in)
                except Exception:
                    pass

            # Output playback: pull frame or output silence
            try:
                raw_out = self._out_queue.get_nowait()
                outdata[:] = raw_out
            except queue.Empty:
                outdata.fill(0)

        # Use latency='low' to trigger low-latency audio driver paths (WASAPI / CoreAudio / ALSA)
        self._stream = sd.RawStream(
            samplerate=SAMPLE_RATE,
            blocksize=SAMPLES_PER_FRAME,
            channels=CHANNELS,
            dtype="int16",
            latency="low",
            callback=audio_callback,
        )
        self._stream.start()

    def stop(self):
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        with self._out_lock:
            if self._wav_out:
                try:
                    self._wav_out.close()
                except Exception:
                    pass
                self._wav_out = None

    def read_frame(self) -> bytes:
        if not self._running:
            return b"\x00" * BYTES_PER_FRAME

        try:
            return self._in_queue.get(timeout=0.03)
        except queue.Empty:
            return b"\x00" * BYTES_PER_FRAME

    def write_frame(self, pcm_data: bytes):
        if not self._running or not pcm_data:
            return

        # Pad or slice to exactly 640 bytes
        if len(pcm_data) < BYTES_PER_FRAME:
            pcm_data = pcm_data.ljust(BYTES_PER_FRAME, b"\x00")
        elif len(pcm_data) > BYTES_PER_FRAME:
            pcm_data = pcm_data[:BYTES_PER_FRAME]

        # Prevent queue latency buildup: if 2 frames are already waiting, drop oldest
        while self._out_queue.qsize() >= 2:
            try:
                self._out_queue.get_nowait()
            except Exception:
                break

        try:
            self._out_queue.put_nowait(pcm_data)
        except queue.Full:
            pass

        with self._out_lock:
            if self._wav_out:
                try:
                    self._wav_out.writeframes(pcm_data)
                except Exception:
                    pass


def get_audio_backend(mode: str = "auto", record_path: str = None, wav_play_path: str = None) -> AudioBackend:
    """
    Factory function to instantiate the appropriate audio backend.
    mode: 'auto', 'live', or 'synth'
    """
    if mode in ("auto", "live"):
        try:
            import sounddevice as sd
            # Test querying devices to verify PortAudio works
            sd.query_devices()
            backend = SounddeviceBackend(record_path=record_path)
            return backend
        except Exception as e:
            if mode == "live":
                raise RuntimeError(f"Live hardware audio requested but sounddevice failed: {e}")
            # In 'auto' mode, fall back cleanly
            pass

    return SyntheticAudioBackend(tone_freq=1000.0, record_path=record_path, wav_play_path=wav_play_path)
