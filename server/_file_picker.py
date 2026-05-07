"""Subprocess helper: show a native OS file/folder picker and print
the selected absolute path to stdout. Empty stdout means the user
cancelled. Run via ``python -m server._file_picker <kind> [initial]``
where ``<kind>`` is ``file`` or ``folder``.

Spawned by ``/api/load/browse`` so the tkinter event loop runs in
its own process — Tk is not thread-safe, and on macOS its main loop
must run on the process's main thread, which conflicts with Flask's.
"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _file_picker <file|folder> [initial_dir]", file=sys.stderr)
        return 2
    kind = sys.argv[1]
    initial = sys.argv[2] if len(sys.argv) > 2 else ""
    if kind not in ("file", "folder"):
        print(f"unknown kind: {kind!r}", file=sys.stderr)
        return 2

    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as e:
        print(f"tkinter not available: {e}", file=sys.stderr)
        return 3

    root = tk.Tk()
    # Hide the empty parent window — we only want the dialog.
    root.withdraw()
    # Force the dialog to the front; without this the dialog can
    # appear behind the browser on Windows.
    root.attributes("-topmost", True)
    root.update()

    if kind == "folder":
        path = filedialog.askdirectory(
            parent=root,
            initialdir=initial or None,
            title="Select a hierarchical WZ pack folder",
            mustexist=True,
        )
    else:
        path = filedialog.askopenfilename(
            parent=root,
            initialdir=initial or None,
            title="Open a .wz file",
            filetypes=[("WZ archives", "*.wz"), ("All files", "*.*")],
        )

    root.destroy()
    # Tk returns an empty string (file) or empty tuple/string (folder) on cancel.
    if path:
        # ``askopenfilename`` returns a forward-slash path on Windows even
        # though native paths use backslashes — leave it; our loader is
        # OS-agnostic and JSON-serializable as-is.
        sys.stdout.write(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
