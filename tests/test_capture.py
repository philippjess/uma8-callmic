import sys
import time

import numpy as np

from uma8_callmic.capture import Capture, record_command


def test_capture_reads_frames_from_command():
    data = np.arange(3 * 4800, dtype=np.float32).reshape(4800, 3)
    script = f"import sys,numpy as n; sys.stdout.buffer.write(n.arange({data.size}, dtype=n.float32).tobytes())"
    cap = Capture("egal", 3, seconds=1.0, command=[sys.executable, "-c", script])
    for _ in range(50):
        if cap.total() >= 4800:
            break
        time.sleep(0.05)
    block = cap.latest(4800)
    cap.close()
    np.testing.assert_array_equal(block, data)


def test_record_command_sets_channel_map_and_no_fallback():
    """Ohne Kanalpositionen nähme pw-record 7.0 und PipeWire mischte um (live geprüft an der AEC-Quelle:
    nur AUX0/1 kamen an)."""
    cmd = record_command("uma8_callmic_aec", 7, positions=tuple(f"AUX{i}" for i in range(7)), no_fallback=True)
    assert cmd[:3] == ["pw-record", "--target", "uma8_callmic_aec"]
    assert cmd[cmd.index("--channels") + 1] == "7"
    assert cmd[cmd.index("--channel-map") + 1] == "AUX0,AUX1,AUX2,AUX3,AUX4,AUX5,AUX6"
    assert cmd[cmd.index("-P") + 1] == "{ node.dont-fallback = true }"
    assert cmd[-4:] == ["--format", "f32", "--raw", "-"]
    plain = record_command("uma8_callmic", 1)
    assert "--channel-map" not in plain and "-P" not in plain


def test_span_reads_absolute_frames():
    data = np.arange(2 * 4800, dtype=np.float32).reshape(4800, 2)
    script = f"import sys,numpy as n; sys.stdout.buffer.write(n.arange({data.size}, dtype=n.float32).tobytes())"
    cap = Capture("egal", 2, seconds=0.05, command=[sys.executable, "-c", script])   # Puffer 2400 Frames
    for _ in range(50):
        if cap.total() >= 4800:
            break
        time.sleep(0.05)
    cap.close()
    np.testing.assert_array_equal(cap.span(4000, 4800), data[4000:])
    assert cap.span(1000, 2000) is None          # schon überschrieben
    assert cap.span(4700, 4900) is None          # noch nicht da
