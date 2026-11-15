"""Start ReTrace for the ring. See ring/service.py.

    .\\.venv\\Scripts\\python.exe retrace_ring.py              normal use
    .\\.venv\\Scripts\\python.exe retrace_ring.py --no-motor   never writes to the ring
    .\\.venv\\Scripts\\python.exe retrace_ring.py weartest --minutes 10
    .\\.venv\\Scripts\\python.exe retrace_ring.py vlm-test image.jpg
    .\\.venv\\Scripts\\python.exe retrace_ring.py replay-holds reports/weartest_…
"""

from ring.service import main

if __name__ == "__main__":
    raise SystemExit(main())
