import sys
import time

import numpy as np

from uma8_callmic.capture import Capture


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
