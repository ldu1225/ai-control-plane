"""AI Control Plane entrypoint.

Run with:  python3 -u src/backend_server.py
The application lives in src/controlplane (see controlplane/app.py).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from controlplane.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
