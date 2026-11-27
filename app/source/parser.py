"""Incremental multipart MJPEG parser and JPEG validator."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import cv2
import numpy as np

_BOUNDARY_RE = re.compile(r'boundary=(?:"([^"]+)"|([^;\s]+))', re.IGNORECASE)
_CONTENT_LENGTH_RE = re.compile(rb"^Content-Length:[ \t]*([0-9]{1,10})[ \t]*\r?$", re.I | re.M)
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


def extract_boundary(content_type: str | None) -> bytes | None:
    """Extract multipart boundary bytes from a Content-Type header."""
    if not content_type:
        return None
    match = _BOUNDARY_RE.search(content_type)
    if not match:
        return None
    raw = (match.group(1) or match.group(2) or "").strip()
    if not raw:
        return None
    if not raw.startswith("--"):
        raw = f"--{raw}"
    return raw.encode("ascii", errors="ignore")


def is_valid_jpeg(data: bytes) -> bool:
    """Verify JPEG markers and ensure OpenCV can decode the image."""
    if len(data) < 4:
        return False
    if not (data.startswith(JPEG_SOI) and data.endswith(JPEG_EOI)):
        return False
    arr = np.frombuffer(data, dtype=np.uint8)
    decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return decoded is not None and decoded.size > 0


@dataclass
class MjpegStreamParser:
    """Stateful incremental parser that extracts complete, valid JPEG frames."""

    boundary: bytes | None = None
    max_buffer_bytes: int = 10 * 1024 * 1024
    validate_decode: bool = True
    _buffer: bytearray = field(default_factory=bytearray, init=False, repr=False)
    malformed_frames: int = field(default=0, init=False)
    buffer_overflows: int = field(default=0, init=False)

    def set_boundary(self, boundary: bytes | None) -> None:
        self.boundary = boundary

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, chunk: bytes) -> list[bytes]:
        """Feed raw bytes from the HTTP stream and return complete JPEG frames."""
        if not chunk:
            return []

        self._buffer.extend(chunk)
        if len(self._buffer) > self.max_buffer_bytes:
            # Keep only the most recent tail to bound memory usage.
            overflow = len(self._buffer) - (self.max_buffer_bytes // 2)
            del self._buffer[:overflow]
            self.buffer_overflows += 1

        if self.boundary:
            return self._extract_with_boundary()
        return self._extract_by_markers()

    def _extract_with_boundary(self) -> list[bytes]:
        frames: list[bytes] = []
        assert self.boundary is not None

        while True:
            start_idx = self._buffer.find(self.boundary)
            if start_idx == -1:
                # Keep a small tail in case the boundary spans chunks.
                keep = max(len(self.boundary), 64)
                if len(self._buffer) > keep:
                    del self._buffer[:-keep]
                break

            if start_idx > 0:
                del self._buffer[:start_idx]

            # Find end of part headers (\r\n\r\n or \n\n)
            header_end = self._buffer.find(b"\r\n\r\n", len(self.boundary))
            sep_len = 4
            if header_end == -1:
                header_end = self._buffer.find(b"\n\n", len(self.boundary))
                sep_len = 2
            if header_end == -1:
                break

            body_start = header_end + sep_len
            length_header = _CONTENT_LENGTH_RE.search(
                self._buffer, len(self.boundary), header_end
            )
            if length_header is not None:
                body_end = body_start + int(length_header[1])
                if (
                    body_start + 4 <= body_end <= len(self._buffer)
                    and self._buffer[body_start : body_start + 2] == JPEG_SOI
                    and self._buffer[body_end - 2 : body_end] == JPEG_EOI
                ):
                    payload = bytes(self._buffer[body_start:body_end])
                    del self._buffer[:body_end]
                    if self._validate(payload):
                        frames.append(payload)
                    else:
                        self.malformed_frames += 1
                    continue

            next_boundary = self._buffer.find(self.boundary, body_start)
            if next_boundary == -1:
                break

            raw_part = bytes(self._buffer[body_start:next_boundary])
            del self._buffer[:next_boundary]

            # Strip trailing CRLF / LF before the next boundary
            payload = raw_part.rstrip(b"\r\n")
            if not payload:
                continue

            # Trim any stray bytes before SOI if present, or validate directly
            soi_pos = payload.find(JPEG_SOI)
            if soi_pos > 0:
                payload = payload[soi_pos:]

            if self._validate(payload):
                frames.append(payload)
            else:
                self.malformed_frames += 1

        return frames

    def _extract_by_markers(self) -> list[bytes]:
        frames: list[bytes] = []
        while True:
            soi = self._buffer.find(JPEG_SOI)
            if soi == -1:
                if len(self._buffer) > 2:
                    del self._buffer[:-2]
                break

            if soi > 0:
                del self._buffer[:soi]

            eoi = self._buffer.find(JPEG_EOI, 2)
            if eoi == -1:
                break

            end = eoi + 2
            payload = bytes(self._buffer[:end])
            del self._buffer[:end]

            if self._validate(payload):
                frames.append(payload)
            else:
                self.malformed_frames += 1

        return frames

    def _validate(self, payload: bytes) -> bool:
        if self.validate_decode:
            return is_valid_jpeg(payload)
        return len(payload) >= 4 and payload.startswith(JPEG_SOI) and payload.endswith(JPEG_EOI)
