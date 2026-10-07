#!/usr/bin/env python3
"""Familiar single-command status entry point; no order calls."""
import sys
from launch import main
from livecore.config import safe_error

if __name__ == '__main__':
    try:
        sys.exit(main(['status']+sys.argv[1:]))
    except Exception as exc:
        print(safe_error(exc),file=sys.stderr)
        sys.exit(1)
