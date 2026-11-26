#!/usr/bin/env python3
"""Start Porthole: share this PC's screen with phones and PCs on the same network."""

import signal
import sys

from porthole.app import main

if __name__ == "__main__":
    code = main()
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # stopped: one more Ctrl+C mustn't print a traceback on the way out
    sys.exit(code)
