"""Timestamped H.264 delivery for the Gira TKS video stream.

The gateway sends H.264 over RTP, but its RTP timestamps are not a usable
90 kHz media clock: on firmware 05.04.00.08 they advance by a fixed 280 ticks
per frame although frames arrive at ~24 fps with one 80 ms gap per GOP.
Delivering a raw Annex-B stream forces the consumer (go2rtc, HA stream) to
stamp frames by arrival time, which turns network/encoder irregularities into
visible judder.

`FrameClock` derives smooth presentation timestamps: RTP timestamp deltas give
the frame order and spacing, and the real tempo is taken from the long-term
arrival rate (robust against start-up gaps).  `MpegTsMuxer` packs every access
unit into MPEG-TS with that PTS so consumers can play it evenly.
"""

from __future__ import annotations

import asyncio
from collections import deque
from statistics import median

TS_PACKET = 188
PAT_PID = 0x0000
PMT_PID = 0x1000
VIDEO_PID = 0x0100
PTS_OFFSET = 90000  # start PTS at 1 s so the PCR below never goes negative
PCR_LEAD = 9000  # PCR runs 100 ms ahead of the PTS of the same frame
AUD = b"\x00\x00\x00\x01\x09\xf0"


class FrameClock:
    """Turn (RTP timestamp, arrival time) pairs into smooth timestamps.

    Every frame advances the clock by `rtp_delta * k`, where `k` (seconds per
    RTP tick) is the arrival time per tick over the recent window, ignoring
    intervals more than `outlier` times the median (start-up stalls).  A small
    `gain` pulls the clock towards arrival time so it never drifts; offsets
    beyond `max_error` seconds (pauses, encoder restarts) re-anchor it.
    """

    def __init__(self, window: int = 200, gain: float = 0.05, max_error: float = 0.5,
                 outlier: float = 3.0) -> None:
        self.window = window
        self.gain = gain
        self.max_error = max_error
        self.outlier = outlier
        self.reset()

    def reset(self) -> None:
        self._last_rtp: int | None = None
        self._last_arrival = 0.0
        self._origin = 0.0
        self._pts = 0.0
        self._intervals: deque[tuple[int, float]] = deque(maxlen=self.window)

    def _seconds_per_tick(self) -> float | None:
        rates = [dt / ticks for ticks, dt in self._intervals if ticks > 0 and dt > 0]
        if not rates:
            return None
        limit = median(rates) * self.outlier
        ticks = sum(t for t, dt in self._intervals if t > 0 and dt > 0 and dt / t <= limit)
        time = sum(dt for t, dt in self._intervals if t > 0 and dt > 0 and dt / t <= limit)
        return time / ticks if ticks else None

    def stamp(self, rtp_timestamp: int, arrival: float) -> float:
        """Return the presentation time (seconds since the first frame)."""
        if self._last_rtp is None:
            self._last_rtp, self._last_arrival, self._origin, self._pts = rtp_timestamp, arrival, arrival, 0.0
            return 0.0
        delta = (rtp_timestamp - self._last_rtp) & 0xFFFFFFFF
        if delta == 0:
            return self._pts
        elapsed = arrival - self._origin
        if delta >= 1 << 31:
            # Timestamp went backwards: new encoder session, re-anchor.
            self._intervals.clear()
            pts = max(elapsed, self._pts + 0.001)
        else:
            self._intervals.append((delta, arrival - self._last_arrival))
            k = self._seconds_per_tick()
            predicted = self._pts + delta * k if k else elapsed
            error = elapsed - predicted
            pts = predicted + self.gain * error if abs(error) <= self.max_error else elapsed
            pts = max(pts, self._pts + 0.001)
        self._last_rtp, self._last_arrival, self._pts = rtp_timestamp, arrival, pts
        return pts


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else (crc << 1)
            crc &= 0xFFFFFFFF
    return crc


def _psi_section(table_id: int, table_ext: int, body: bytes) -> bytes:
    length = 5 + len(body) + 4  # header after length field + body + CRC
    section = bytes((table_id, 0xB0 | (length >> 8), length & 0xFF,
                     table_ext >> 8, table_ext & 0xFF, 0xC1, 0x00, 0x00)) + body
    return section + _crc32_mpeg(section).to_bytes(4, "big")


PAT_SECTION = _psi_section(0x00, 0x0001, bytes((0x00, 0x01, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF)))
PMT_SECTION = _psi_section(0x02, 0x0001, bytes((
    0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF,  # PCR PID
    0xF0, 0x00,  # program_info_length 0
    0x1B, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00,  # H.264 stream
)))


def _pts_bytes(pts: int) -> bytes:
    pts &= (1 << 33) - 1
    return bytes((
        0x21 | ((pts >> 29) & 0x0E),
        (pts >> 22) & 0xFF,
        0x01 | ((pts >> 14) & 0xFE),
        (pts >> 7) & 0xFF,
        0x01 | ((pts << 1) & 0xFE),
    ))


def _pcr_bytes(pcr_base: int) -> bytes:
    pcr_base &= (1 << 33) - 1
    return bytes((
        (pcr_base >> 25) & 0xFF, (pcr_base >> 17) & 0xFF, (pcr_base >> 9) & 0xFF,
        (pcr_base >> 1) & 0xFF, ((pcr_base & 1) << 7) | 0x7E, 0x00,
    ))


class MpegTsMuxer:
    """Minimal single-program MPEG-TS muxer for one H.264 elementary stream."""

    def __init__(self) -> None:
        self._cc: dict[int, int] = {}

    def reset(self) -> None:
        self._cc.clear()

    def _next_cc(self, pid: int) -> int:
        cc = self._cc.get(pid, -1) + 1 & 0x0F
        self._cc[pid] = cc
        return cc

    def _psi_packet(self, pid: int, section: bytes) -> bytes:
        header = bytes((0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x10 | self._next_cc(pid)))
        payload = b"\x00" + section
        return header + payload + b"\xff" * (TS_PACKET - 4 - len(payload))

    def tables(self) -> bytes:
        return self._psi_packet(PAT_PID, PAT_SECTION) + self._psi_packet(PMT_PID, PMT_SECTION)

    def mux(self, access_unit: bytes, pts_seconds: float, keyframe: bool) -> bytes:
        """Return TS packets for one Annex-B access unit (tables before keyframes)."""
        pts = PTS_OFFSET + round(pts_seconds * 90000)
        es = access_unit if access_unit.startswith(AUD) else AUD + access_unit
        pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + _pts_bytes(pts) + es
        out = bytearray(self.tables() if keyframe else b"")
        pos = 0
        first = True
        while pos < len(pes):
            flags = 0
            af_body = b""
            if first:
                flags = 0x10 | (0x40 if keyframe else 0)  # PCR, random access
                af_body = bytes((flags,)) + _pcr_bytes(pts - PCR_LEAD)
            room = TS_PACKET - 4 - (1 + len(af_body) if af_body else 0)
            chunk = pes[pos:pos + room]
            stuffing = room - len(chunk)
            if stuffing > 0:
                if af_body:
                    af_body += b"\xff" * stuffing
                elif stuffing == 1:
                    af_body = None  # adaptation_field_length 0
                else:
                    af_body = b"\x00" + b"\xff" * (stuffing - 2)
            has_af = af_body is None or bool(af_body)
            header = bytes((0x47, (0x40 if first else 0x00) | (VIDEO_PID >> 8), VIDEO_PID & 0xFF,
                            (0x30 if has_af else 0x10) | self._next_cc(VIDEO_PID)))
            if af_body is None:
                adaptation = b"\x00"
            elif af_body:
                adaptation = bytes((len(af_body),)) + af_body
            else:
                adaptation = b""
            packet = header + adaptation + chunk
            assert len(packet) == TS_PACKET, len(packet)
            out += packet
            pos += len(chunk)
            first = False
        return bytes(out)


class StreamChannel:
    """Fan-out of keyframe-aligned chunks to HTTP viewers (one queue each).

    Keeps everything since the last keyframe so a new viewer can start
    decoding at once; a viewer whose queue overflows is resynchronised at the
    next keyframe.  Same semantics as the raw Annex-B stream.
    """

    def __init__(self, queue_size: int, max_buffer: int = 8 * 1024 * 1024) -> None:
        self.queue_size = queue_size
        self.max_buffer = max_buffer
        self.clients: set = set()
        self.resync: set = set()
        self.buffer: deque[bytes] = deque()
        self.buffer_bytes = 0
        self.buffer_valid = False
        self.started = False
        self.drops = 0

    def clear(self) -> None:
        self.buffer.clear()
        self.buffer_bytes = 0
        self.buffer_valid = False
        self.started = False

    def publish(self, chunk: bytes, keyframe: bool) -> None:
        if keyframe:
            self.clear()
            self.buffer_valid = self.started = True
        elif not self.started:
            return  # nothing decodable before the first keyframe
        if self.buffer_valid:
            self.buffer.append(chunk)
            self.buffer_bytes += len(chunk)
            if self.buffer_bytes > self.max_buffer:
                # Only the catch-up buffer for *new* viewers is given up (they
                # wait for the next keyframe); running viewers keep receiving.
                self.buffer.clear()
                self.buffer_bytes = 0
                self.buffer_valid = False
        for queue in tuple(self.clients):
            if queue in self.resync:
                if not keyframe:
                    continue
                self.resync.discard(queue)
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                    self.drops += 1
                if not keyframe:
                    self.resync.add(queue)
                    continue
            queue.put_nowait(chunk)

    def close_all(self) -> None:
        """Wake every viewer with an empty chunk (end of stream)."""
        self.resync.clear()
        for queue in tuple(self.clients):
            if queue.full():
                try:
                    queue.get_nowait()
                    self.drops += 1
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(b"")
        self.clear()
