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


def calculate_rms_db(pcm_data: bytes) -> float:
    """
    Computes RMS level in dBFS for 16-bit signed PCM data.
    Returns value between -96.0 dBFS and 0.0 dBFS.
    """
    if not pcm_data or len(pcm_data) < 2:
        return -96.0

    count = len(pcm_data) // 2
    # Unpack 16-bit signed integers
    fmt = f"<{count}h"
    try:
        samples = struct.unpack(fmt, pcm_data[:count * 2])
    except struct.error:
        return -96.0

    sum_squares = sum(s * s for s in samples)
    mean_square = sum_squares / count
    if mean_square <= 0:
        return -96.0

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
            frames.append(struct.pack(f"<{SAMPLES_PER_FRAME}h", *samples))
            samples = []

    if samples:
        samples += [0] * (SAMPLES_PER_FRAME - len(samples))
        frames.append(struct.pack(f"<{SAMPLES_PER_FRAME}h", *samples))

    return frames


class AudioBackend:
    """Abstract base class for audio backends."""

    def start(self):
        """Start audio streams or generators."""
        raise NotImplementedError

    def stop(self):
        """Stop audio streams or generators and clean up resources."""
        raise NotImplementedError

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
    Captures live microphone input and streams output to speakers.
    """

    def __init__(self, record_path: str = None):
        self.record_path = record_path
        self._running = False
        self._stream = None
        self._in_queue = queue.Queue(maxsize=50)
        self._out_queue = queue.Queue(maxsize=50)
        self._wav_out = None
        self._out_lock = threading.Lock()

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
            # Input capture
            raw_in = bytes(indata)
            try:
                self._in_queue.put_nowait(raw_in)
            except queue.Full:
                try:
                    self._in_queue.get_nowait()
                    self._in_queue.put_nowait(raw_in)
                except Exception:
                    pass

            # Output playback
            try:
                raw_out = self._out_queue.get_nowait()
                outdata[:] = raw_out
            except queue.Empty:
                outdata.fill(0)

        self._stream = sd.RawStream(
            samplerate=SAMPLE_RATE,
            blocksize=SAMPLES_PER_FRAME,
            channels=CHANNELS,
            dtype="int16",
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
            return self._in_queue.get(timeout=0.05)
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

        try:
            self._out_queue.put_nowait(pcm_data)
        except queue.Full:
            try:
                self._out_queue.get_nowait()
                self._out_queue.put_nowait(pcm_data)
            except Exception:
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
