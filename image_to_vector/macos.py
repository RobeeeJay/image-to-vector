"""macOS-only: reading files dropped from privacy-protected folders.

Files in protected folders (Messages attachments, for one) are readable only
by an app the user dropped them on, and that grant travels with the NSURL on
the drag pasteboard. Qt takes just the path string, so the grant is lost and
the read fails with "Operation not permitted". The grant covers only the
process that consumes it, which is why the trace worker is sent file
contents rather than a path.
"""
from contextlib import contextmanager

from AppKit import NSURL, NSPasteboard, NSPasteboardNameDrag


@contextmanager
def dropped_file_access():
    """Hold the access grants of the drag in progress; call from a drop handler."""
    pasteboard = NSPasteboard.pasteboardWithName_(NSPasteboardNameDrag)
    urls = pasteboard.readObjectsForClasses_options_([NSURL], None) or []
    started = [url for url in urls if url.startAccessingSecurityScopedResource()]
    try:
        yield
    finally:
        for url in started:
            url.stopAccessingSecurityScopedResource()
