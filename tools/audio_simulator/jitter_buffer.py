"""
RoIP Audio Jitter Buffer (jitter_buffer.py)
A thread-safe jitter buffer and squelch detector for 20ms 16-bit PCM frames.
"""

import collections
import threading
import time
from .audio_backend import BYTES_PER_FRAME


class JitterBuffer:
    """
    Circular queue that absorbs network arrival jitter for 20ms RoIP audio frames.
    Also tracks carrier-operated squelch (COS) activity based on packet arrival timing.
    """

    def __init__(self, target_delay_frames: int = 2, max_frames: int = 15, squelch_timeout_s: float = 0.15):
        """
        target_delay_frames: Number of frames to accumulate before initiating playback (e.g. 2 frames = 40ms).
        max_frames: Maximum allowable frames in buffer before dropping oldest (prevents runaway latency).
        squelch_timeout_s: Duration without packets before COS is considered inactive.
        """
        self.target_delay_frames = target_delay_frames
        self.max_frames = max_frames
        self.squelch_timeout_s = squelch_timeout_s

        self._queue = collections.deque()
        self._lock = threading.Lock()
        self._last_packet_time = 0.0
        self._buffering = True

        # Statistics
        self.total_frames_received = 0
        self.total_frames_played = 0
        self.underrun_count = 0
        self.overflow_drop_count = 0
        self.last_seq = -1

    def push(self, seq: int, pcm_data: bytes, timestamp: int = 0):
        """Adds a newly arrived RoIP PCM frame to the buffer."""
        now = time.time()
        with self._lock:
            self._last_packet_time = now
            self.total_frames_received += 1
            self.last_seq = seq

            # Prevent buffer overflow
            if len(self._queue) >= self.max_frames:
                self._queue.popleft()
                self.overflow_drop_count += 1

            self._queue.append((seq, pcm_data, timestamp))

            # Stop buffering once we reach target depth
            if self._buffering and len(self._queue) >= self.target_delay_frames:
                self._buffering = False

    def pop(self) -> bytes:
        """
        Pulls the next 640-byte PCM frame for playback.
        Returns 640 bytes of silence if the buffer is empty or still buffering.
        """
        silence = b"\x00" * BYTES_PER_FRAME
        with self._lock:
            # If idle / squelch timed out, reset buffering
            if time.time() - self._last_packet_time > self.squelch_timeout_s:
                self._buffering = True

            if self._buffering:
                return silence

            if self._queue:
                seq, data, ts = self._queue.popleft()
                self.total_frames_played += 1
                return data
            else:
                # Buffer underrun
                self.underrun_count += 1
                self._buffering = True
                return silence

    @property
    def is_receiving(self) -> bool:
        """Returns True if audio packets are actively arriving (COS active)."""
        with self._lock:
            return (time.time() - self._last_packet_time) <= self.squelch_timeout_s

    @property
    def queued_frames(self) -> int:
        with self._lock:
            return len(self._queue)

    def reset(self):
        with self._lock:
            self._queue.clear()
            self._buffering = True
