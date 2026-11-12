"""Tests for timestamped MPEG-TS delivery (tks_video)."""

from __future__ import annotations

import asyncio
import importlib.util
import random
import statistics
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "tks_video",
    Path(__file__).resolve().parents[1] / "custom_components" / "gira_tks" / "tks_video.py",
)
tv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tv)


def _gateway_pattern(frames: int, start_ts: int = 1_000_000):
    """Pattern measured on firmware 05.04.00.08: +280 ticks per frame, frames
    every 40 ms, one 80 ms gap every 25 frames, a 510 ms stall at start-up."""
    ts, t = start_ts, 100.0
    out = [(ts & 0xFFFFFFFF, t)]
    for i in range(1, frames):
        ts += 280
        t += 0.510 if i == 2 else (0.080 if i % 25 == 0 else 0.040)
        out.append((ts & 0xFFFFFFFF, t))
    return out


def _stamp_all(clock, frames):
    return [clock.stamp(ts, t) for ts, t in frames]


def test_gateway_pattern_is_smoothed():
    frames = _gateway_pattern(600)
    pts = _stamp_all(tv.FrameClock(), frames)
    steps = [(b - a) * 1000 for a, b in zip(pts[100:], pts[101:])]
    arrival = [(b[1] - a[1]) * 1000 for a, b in zip(frames[100:], frames[101:])]
    assert statistics.pstdev(arrival) > 7  # the input judders (1 Hz 80 ms gaps)
    assert statistics.pstdev(steps) < 1.5  # the output does not
    assert max(steps) < 45
    # real tempo: 25 frames per 1.04 s → 41.6 ms per frame, no drift
    assert statistics.mean(steps) == pytest.approx(41.6, abs=0.3)
    offset = [(t - frames[0][1]) - p for (_, t), p in zip(frames, pts)]
    assert max(offset[100:]) - min(offset[100:]) < 0.15


def test_regular_90khz_source_with_network_jitter():
    rnd = random.Random(1)
    ts, frames = 0, []
    for i in range(500):
        frames.append((ts, 50.0 + i * 0.040 + rnd.uniform(-0.012, 0.012)))
        ts += 3600
    pts = _stamp_all(tv.FrameClock(), frames)
    steps = [(b - a) * 1000 for a, b in zip(pts[50:], pts[51:])]
    assert statistics.mean(steps) == pytest.approx(40.0, abs=0.3)
    assert statistics.pstdev(steps) < 2.0


def test_rtp_timestamp_wraparound_is_continuous():
    frames = _gateway_pattern(200, start_ts=(1 << 32) - 280 * 50)
    assert frames[60][0] < frames[40][0]  # wrapped
    pts = _stamp_all(tv.FrameClock(), frames)
    steps = [b - a for a, b in zip(pts, pts[1:])]
    assert all(0 < s < 0.6 for s in steps)
    assert max(steps[60:]) < 0.045


def test_backwards_timestamp_reanchors_monotonically():
    clock = tv.FrameClock()
    pts = [clock.stamp(1000 + 280 * i, 10 + 0.04 * i) for i in range(50)]
    pts.append(clock.stamp(5, 10 + 0.04 * 50))  # encoder restart
    pts.append(clock.stamp(285, 10 + 0.04 * 51))
    assert all(b > a for a, b in zip(pts, pts[1:]))


def test_large_tick_jump_without_time_gap_resyncs():
    clock = tv.FrameClock()
    pts = [clock.stamp(280 * i, 0.04 * i) for i in range(100)]
    jump = clock.stamp(280 * 99 + 8960, 0.04 * 100)  # 32 frames skipped in the counter
    assert jump - pts[-1] < 0.1  # follows arrival instead of jumping 1.3 s ahead


# --- MPEG-TS ---------------------------------------------------------------

SPS = b"\x00\x00\x00\x01\x67\x42\x00\x1e\xab\x40\x50\x1e\xc8"
PPS = b"\x00\x00\x00\x01\x68\xce\x38\x80"
IDR = b"\x00\x00\x00\x01\x65" + bytes(range(256)) * 12
P = b"\x00\x00\x00\x01\x41" + b"\x9a" * 700


def _packets(data: bytes):
    assert len(data) % 188 == 0
    return [data[i:i + 188] for i in range(0, len(data), 188)]


def _pid(pkt):
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def _payload(pkt):
    afc = (pkt[3] >> 4) & 3
    pos = 4
    if afc & 2:
        pos += 1 + pkt[4]
    return pkt[pos:] if afc & 1 else b""


def _decode_pts(b: bytes) -> int:
    return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def test_psi_tables_have_valid_crc():
    for section in (tv.PAT_SECTION, tv.PMT_SECTION):
        assert tv._crc32_mpeg(section) == 0  # CRC over data+CRC is zero


def test_keyframe_packetization():
    mux = tv.MpegTsMuxer()
    data = mux.mux(SPS + PPS + IDR, 1.5, keyframe=True)
    pkts = _packets(data)
    assert all(p[0] == 0x47 for p in pkts)
    assert _pid(pkts[0]) == tv.PAT_PID and _pid(pkts[1]) == tv.PMT_PID
    video = [p for p in pkts if _pid(p) == tv.VIDEO_PID]
    first = video[0]
    assert first[1] & 0x40  # payload_unit_start
    assert first[3] & 0x20 and first[5] & 0x50 == 0x50  # adaptation field with PCR + random access
    pes = b"".join(_payload(p) for p in video)
    assert pes.startswith(b"\x00\x00\x01\xe0")
    assert _decode_pts(pes[9:14]) == tv.PTS_OFFSET + 135000
    assert pes[14:].startswith(tv.AUD) and pes[14 + len(tv.AUD):] == SPS + PPS + IDR
    ccs = [p[3] & 0x0F for p in video]
    assert ccs == [i & 0x0F for i in range(len(video))]


def test_continuity_and_tables_only_on_keyframes():
    mux = tv.MpegTsMuxer()
    a = _packets(mux.mux(SPS + PPS + IDR, 0.0, True))
    b = _packets(mux.mux(P, 0.0416, False))
    assert all(_pid(p) == tv.VIDEO_PID for p in b)
    last_cc = [p[3] & 0x0F for p in a if _pid(p) == tv.VIDEO_PID][-1]
    assert b[0][3] & 0x0F == (last_cc + 1) & 0x0F
    assert not b[0][5] & 0x40  # no random access flag on P frames


@pytest.mark.parametrize("size", [1, 150, 160, 168, 169, 170, 171, 182, 183, 184, 185, 366, 367, 2000])
def test_every_payload_size_fills_whole_packets(size):
    mux = tv.MpegTsMuxer()
    nal = b"\x00\x00\x00\x01\x41" + b"\x11" * size
    pkts = _packets(mux.mux(nal, 0.0, False))
    pes = b"".join(_payload(p) for p in pkts)
    assert pes.endswith(nal)


# --- StreamChannel -----------------------------------------------------------

def test_channel_buffers_from_keyframe_and_resyncs_slow_viewers():
    ch = tv.StreamChannel(queue_size=2)
    ch.publish(b"p0", False)  # before any keyframe: dropped
    assert not ch.buffer
    ch.publish(b"k1", True)
    ch.publish(b"p1", False)
    assert list(ch.buffer) == [b"k1", b"p1"]
    q = asyncio.Queue(maxsize=2)
    ch.clients.add(q)
    ch.publish(b"p2", False)
    ch.publish(b"p3", False)
    ch.publish(b"p4", False)  # overflow → drop and wait for keyframe
    assert q.empty() and q in ch.resync and ch.drops == 2
    ch.publish(b"p5", False)
    assert q.empty()
    ch.publish(b"k2", True)
    assert q.get_nowait() == b"k2" and q not in ch.resync
    ch.close_all()
    assert q.get_nowait() == b"" and not ch.buffer


def test_buffer_overflow_does_not_stop_running_viewers():
    """Regression: a full catch-up buffer used to stop delivery until the next
    IDR (~10 s on the TKS gateway), causing 6 s freezes."""
    ch = tv.StreamChannel(queue_size=1000, max_buffer=50)
    q = asyncio.Queue(maxsize=1000)
    ch.clients.add(q)
    ch.publish(b"K" * 20, True)
    for i in range(10):
        ch.publish(b"P" * 20, False)  # exceeds max_buffer after two frames
    assert q.qsize() == 11  # every frame delivered
    assert not ch.buffer_valid and not ch.buffer  # only the catch-up buffer is gone
    late = asyncio.Queue(maxsize=1000)
    ch.clients.add(late)
    ch.resync.add(late)  # a new viewer without buffer waits for the next keyframe
    ch.publish(b"P", False)
    assert late.empty()
    ch.publish(b"K2", True)
    assert late.get_nowait() == b"K2" and ch.buffer_valid
