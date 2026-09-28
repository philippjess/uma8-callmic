"""Array-Geometrie. Muss exakt Geometry::positions() in plugin/src/beam.rs entsprechen."""
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ArrayGeometry:
    center: int = 0
    ring: tuple[int, ...] = (1, 6, 5, 4, 3, 2)
    ring_offset_deg: float = 90.0
    radius_m: float = 0.043

    def is_valid(self) -> bool:
        channels = [self.center, *self.ring]
        return len(self.ring) == 6 and sorted(channels) == list(range(7)) and self.radius_m > 0

    def positions(self) -> np.ndarray:
        """(7, 3): Position je Kanal in Metern, z = 0."""
        p = np.zeros((7, 3))
        for k, ch in enumerate(self.ring):
            a = np.radians(self.ring_offset_deg + 60.0 * k)
            p[ch, 0] = self.radius_m * np.cos(a)
            p[ch, 1] = self.radius_m * np.sin(a)
        return p


UMA8 = ArrayGeometry()
