"""Print the laptop environment ReTrace needs for DINOv2 and wear tests.

Run from the project root:
    .\\.venv\\Scripts\\python.exe tools\\envcheck.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Suggested CUDA wheel for Windows + RTX 30-series on Python 3.14 may lag;
# print a concrete command that works on common PyTorch CUDA 12.x builds.
TORCH_CUDA_PIP = (
    r'.\.venv\Scripts\python.exe -m pip install torch torchvision '
    r'--index-url https://download.pytorch.org/whl/cu126'
)


def recordings_root() -> Path:
    try:
        from ring.config import load_config
        from ring.service import CONFIG_PATH, DATA_DIR

        config = load_config(CONFIG_PATH if CONFIG_PATH.is_file() else None)
        if getattr(config, "data_dir", ""):
            return Path(config.data_dir).expanduser().resolve()
        return Path(DATA_DIR).resolve()
    except Exception:
        return (ROOT / "recordings" / "ring_memory").resolve()


def main() -> int:
    print("ReTrace environment check")
    print(f"  Python:        {sys.version.split()[0]} ({sys.executable})")
    try:
        import torch

        print(f"  torch:         {torch.__version__}")
        cuda = bool(torch.cuda.is_available())
        print(f"  torch.cuda:    {cuda}")
        if cuda:
            props = torch.cuda.get_device_properties(0)
            free_b, total_b = torch.cuda.mem_get_info(0)
            print(f"  GPU:           {torch.cuda.get_device_name(0)}")
            print(f"  VRAM total:    {total_b / (1024 ** 3):.2f} GB")
            print(f"  VRAM free:     {free_b / (1024 ** 3):.2f} GB")
            print(f"  GPU compute:   {props.major}.{props.minor}")
        else:
            print("  GPU:           (CUDA not available to this torch build)")
            print(f"  Install CUDA torch with:")
            print(f"    {TORCH_CUDA_PIP}")
    except ImportError:
        print("  torch:         not installed")
        print("  Install CUDA torch with:")
        print(f"    {TORCH_CUDA_PIP}")
        print("  Or CPU-only (slower embeddings):")
        print(r"    .\.venv\Scripts\python.exe -m pip install torch torchvision")

    data = recordings_root()
    drive = data.drive or str(data.anchor)
    usage = shutil.disk_usage(drive if drive else str(data))
    print(f"  recordings:    {data}")
    print(f"  disk free:     {usage.free / (1024 ** 3):.2f} GB free of {usage.total / (1024 ** 3):.2f} GB on {drive or data}")
    print()
    print("To put the visual memory on another drive, set in ring_config.json:")
    print(json.dumps({"data_dir": "D:/retrace_memory"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
