"""Terminal QR codes for invites and processor cards."""
from __future__ import annotations

import io
import sys


def print_qr(text: str, out=None) -> None:
    out = out or sys.stdout
    try:
        import segno
    except ImportError:
        print("(install segno to show a QR code)", file=out)
        return
    buf = io.StringIO()
    segno.make(text, error="m", micro=False).terminal(out=buf, compact=True, border=2)
    out.write(buf.getvalue())
