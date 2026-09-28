import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def plugin_so() -> str:
    subprocess.run(
        ["cargo", "build", "--release", "--quiet", "--manifest-path", str(ROOT / "plugin/Cargo.toml")],
        check=True,
    )
    return str(ROOT / "plugin/target/release/libuma8_beam.so")
