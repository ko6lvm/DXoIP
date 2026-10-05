"""
RoIP Audio Jitter Buffer (jitter_buffer.py)
A thread-safe jitter buffer and squelch detector for 20ms 16-bit PCM frames.
"""

import collections
import threading
import time
from .audio_backend import BYTES_PER_FRAME


SILENCE_FRAME = b"\x00" * BYTES_PER_FRAME


def seq_diff(a: int, b: int) -> int:
    """
    Returns signed sequence difference (a - b) modulo 65536.
    Positive if 'a' is ahead of 'b', negative if 'a' is behind 'b'.
    """
    diff = (a - b) & 0xFFFF
    return diff if diff < 0x8000 else diff - 0x10000


class JitterBuffer:
    """
    Circular queue that absorbs network arrival jitter for 20ms RoIP audio frames.
    Maintains packet sequence ordering and tracks carrier-operated squelch (COS) activity.
    """

    def __init__(self, target_delay_frames: int = 1, max_frames: int = 4, squelch_timeout_s: float = 0.15):
        """
        target_delay_frames: Number of frames to accumulate before initiating playback (e.g. 1 frame = 20ms).
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
        self._last_played_seq = None

        # Statistics
        self.total_frames_received = 0
        self.total_frames_played = 0
        self.underrun_count = 0
        self.overflow_drop_count = 0
        self.late_drop_count = 0
        self.last_seq = -1

    def push(self, seq: int, pcm_data: bytes, timestamp: int = 0):
        """Adds a newly arrived RoIP PCM frame to the buffer in sequence order."""
        now = time.time()
        with self._lock:
            # If transmission was idle, reset squelch sequence tracking
            if now - self._last_packet_time > self.squelch_timeout_s:
                self._last_played_seq = None

            self._last_packet_time = now
            self.total_frames_received += 1
            self.last_seq = seq

            # Drop late packets that arrive after their playback deadline
            if self._last_played_seq is not None and seq_diff(seq, self._last_played_seq) <= 0:
                self.late_drop_count += 1
                return

            # Check for duplicates or insert in sequence order
            if not self._queue or seq_diff(seq, self._queue[-1][0]) > 0:
                self._queue.append((seq, pcm_data, timestamp))
            elif seq_diff(seq, self._queue[0][0]) < 0:
                self._queue.appendleft((seq, pcm_data, timestamp))
            else:
                # Insert at sorted position
                inserted = False
                for idx, (item_seq, _, _) in enumerate(self._queue):
                    diff = seq_diff(seq, item_seq)
                    if diff == 0:
                        # Duplicate packet: ignore
                        inserted = True
                        break
                    elif diff < 0:
                        self._queue.insert(idx, (seq, pcm_data, timestamp))
                        inserted = True
                        break
                if not inserted:
                    self._queue.append((seq, pcm_data, timestamp))

            # Prevent buffer overflow
            while len(self._queue) > self.max_frames:
                self._queue.popleft()
                self.overflow_drop_count += 1

            # Stop buffering once we reach target depth
            if self._buffering and len(self._queue) >= self.target_delay_frames:
                self._buffering = False

    def pop(self) -> bytes:
        """
        Pulls the next 640-byte PCM frame for playback.
        Returns 640 bytes of silence if the buffer is empty or still buffering.
        """
        with self._lock:
            # If idle / squelch timed out, reset buffering
            if time.time() - self._last_packet_time > self.squelch_timeout_s:
                self._buffering = True
                self._last_played_seq = None

            if self._buffering:
                return SILENCE_FRAME

            if self._queue:
                seq, data, ts = self._queue.popleft()
                self._last_played_seq = seq
                self.total_frames_played += 1
                return data
            else:
                # Buffer underrun
                self.underrun_count += 1
                self._buffering = True
                return SILENCE_FRAME

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
            self._last_played_seq = None
