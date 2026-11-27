"""Visual analysis: perceptual hashing (pHash) and structural similarity (SSIM)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from app.config import CropConfig, UploadCropConfig


def decode_jpeg(jpeg_bytes: bytes) -> np.ndarray | None:
    """Decode raw JPEG bytes into a BGR OpenCV image."""
    if not jpeg_bytes:
        return None
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        return None
    return img


def crop_image(
    image: np.ndarray,
    *,
    left: float,
    top: float,
    right: float,
    bottom: float,
) -> np.ndarray:
    """Crop a BGR/grayscale image using normalized [0.0, 1.0] coordinates."""
    h, w = image.shape[:2]
    x1 = max(0, min(w - 1, int(round(w * left))))
    x2 = max(x1 + 1, min(w, int(round(w * right))))
    y1 = max(0, min(h - 1, int(round(h * top))))
    y2 = max(y1 + 1, min(h, int(round(h * bottom))))
    return image[y1:y2, x1:x2]


def _downscale_if_needed(image: np.ndarray, max_dim: int | None) -> np.ndarray:
    """Resize image proportionally so its longest side does not exceed max_dim."""
    if max_dim is None:
        return image
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest <= max_dim:
        return image
    scale = max_dim / float(longest)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)


def encode_jpeg(
    image: np.ndarray,
    quality: int = 75,
    max_dim: int | None = None,
    optimize: bool = True,
) -> bytes:
    """Optionally downscale and encode an OpenCV image into JPEG bytes."""
    target = _downscale_if_needed(image, max_dim)
    ok, buf = cv2.imencode(
        ".jpg",
        target,
        [
            cv2.IMWRITE_JPEG_QUALITY,
            quality,
            cv2.IMWRITE_JPEG_OPTIMIZE,
            1 if optimize else 0,
        ],
    )
    if not ok:
        raise ValueError("Failed to encode image as JPEG")
    return buf.tobytes()


def encode_png(
    image: np.ndarray,
    max_dim: int | None = None,
) -> bytes:
    """Optionally downscale and encode an OpenCV image into PNG bytes."""
    target = _downscale_if_needed(image, max_dim)
    ok, buf = cv2.imencode(".png", target)
    if not ok:
        raise ValueError("Failed to encode image as PNG")
    return buf.tobytes()


def encode_image(
    image: np.ndarray,
    *,
    fmt: str = "jpeg",
    quality: int = 75,
    max_dim: int | None = None,
    optimize: bool = True,
) -> tuple[bytes, str]:
    """Encode an OpenCV image in the requested format and return (bytes, media_type)."""
    normalized = fmt.strip().lower()
    if normalized == "png":
        return encode_png(image, max_dim=max_dim), "image/png"
    if normalized in ("jpg", "jpeg"):
        return (
            encode_jpeg(image, quality=quality, max_dim=max_dim, optimize=optimize),
            "image/jpeg",
        )
    raise ValueError(f"Unsupported image format: {fmt}")


def detect_content_bounds(
    image: np.ndarray,
    *,
    padding: float = 0.02,
    min_area_ratio: float = 0.015,
    max_area_ratio: float = 0.92,
) -> tuple[float, float, float, float] | None:
    """Detect foreground content bounds (text, options, diagrams) on light or dark backgrounds.

    Returns normalized (left, top, right, bottom) or None if a reliable crop
    cannot be established.
    """
    if image.size == 0:
        return None
    h, w = image.shape[:2]
    if h < 16 or w < 16:
        return None

    # Downsample moderately while preserving thin small-text and diagram strokes.
    scale = min(1.0, 960.0 / float(max(h, w)))
    small = (
        cv2.resize(
            image,
            (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
            interpolation=cv2.INTER_AREA,
        )
        if scale < 1.0
        else image
    )
    sh, sw = small.shape[:2]

    # Estimate background color from outer border pixels (works for light & dark mode).
    border = np.concatenate(
        [small[0, :], small[-1, :], small[:, 0], small[:, -1]],
        axis=0,
    )
    bg_color = np.median(border, axis=0).astype(np.int16)

    if small.ndim == 3:
        diff = np.max(np.abs(small.astype(np.int16) - bg_color), axis=2)
    else:
        diff = np.abs(small.astype(np.int16) - int(bg_color))

    mask = (diff > 12).astype(np.uint8) * 255
    kernel_w = max(7, int(round(sw * 0.03)))
    kernel_h = max(7, int(round(sh * 0.03)))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, kernel_h))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    ys, xs = np.where(closed > 0)
    if xs.size == 0 or ys.size == 0:
        return None

    x1 = int(xs.min())
    x2 = int(xs.max()) + 1
    y1 = int(ys.min())
    y2 = int(ys.max()) + 1

    area_ratio = ((x2 - x1) * (y2 - y1)) / float(sw * sh)
    if area_ratio < min_area_ratio or area_ratio > max_area_ratio:
        return None

    left = max(0.0, (x1 / float(sw)) - padding)
    top = max(0.0, (y1 / float(sh)) - padding)
    right = min(1.0, (x2 / float(sw)) + padding)
    bottom = min(1.0, (y2 / float(sh)) + padding)
    if left >= right or top >= bottom:
        return None
    return (left, top, right, bottom)


def prepare_upload_image(
    full_image: np.ndarray,
    *,
    detection_crop: CropConfig | None,
    upload_crop: UploadCropConfig | None,
    detection_image: np.ndarray | None = None,
) -> tuple[bytes | None, str, bool]:
    """Prepare encoded upload image bytes from the exact accepted frame.

    Returns (encoded_bytes_or_none, media_type, used_wider_fallback).
    When both detection_crop and upload_crop are None (or mode='inherit' with
    detection_crop=None), returns (None, 'image/jpeg', False) so callers can
    forward the original frame bytes unchanged.
    """
    if upload_crop is None or upload_crop.mode == "inherit":
        if detection_crop is None:
            return None, "image/jpeg", False
        target = (
            detection_image
            if detection_image is not None
            else crop_image(
                full_image,
                left=detection_crop.left,
                top=detection_crop.top,
                right=detection_crop.right,
                bottom=detection_crop.bottom,
            )
        )
        fmt = upload_crop.format if upload_crop is not None else "jpeg"
        optimize = upload_crop.jpeg_optimize if upload_crop is not None else True
        quality = (
            upload_crop.jpeg_quality
            if upload_crop is not None
            else detection_crop.jpeg_quality
        )
        max_dim = (
            upload_crop.max_dim
            if upload_crop is not None
            else detection_crop.max_dim
        )
        data, media_type = encode_image(
            target, fmt=fmt, quality=quality, max_dim=max_dim, optimize=optimize
        )
        return data, media_type, False

    if upload_crop.mode == "full":
        data, media_type = encode_image(
            full_image,
            fmt=upload_crop.format,
            quality=upload_crop.jpeg_quality,
            max_dim=upload_crop.max_dim,
            optimize=upload_crop.jpeg_optimize,
        )
        return data, media_type, False

    if upload_crop.mode == "fixed":
        left = max(0.0, upload_crop.left - upload_crop.padding)
        top = max(0.0, upload_crop.top - upload_crop.padding)
        right = min(1.0, upload_crop.right + upload_crop.padding)
        bottom = min(1.0, upload_crop.bottom + upload_crop.padding)
        cropped = crop_image(full_image, left=left, top=top, right=right, bottom=bottom)
        data, media_type = encode_image(
            cropped,
            fmt=upload_crop.format,
            quality=upload_crop.jpeg_quality,
            max_dim=upload_crop.max_dim,
            optimize=upload_crop.jpeg_optimize,
        )
        return data, media_type, False

    # mode == "content"
    bounds = detect_content_bounds(
        full_image,
        padding=upload_crop.padding,
        min_area_ratio=upload_crop.min_content_area_ratio,
        max_area_ratio=upload_crop.max_content_area_ratio,
    )
    used_fallback = False
    if bounds is not None:
        left, top, right, bottom = bounds
        target = crop_image(full_image, left=left, top=top, right=right, bottom=bottom)
    elif upload_crop.fallback_to_wider:
        used_fallback = True
        if detection_crop is not None:
            # Widen slightly around detection_crop so figures/edges stay visible.
            pad = max(upload_crop.padding, 0.04)
            target = crop_image(
                full_image,
                left=max(0.0, detection_crop.left - pad),
                top=max(0.0, detection_crop.top - pad),
                right=min(1.0, detection_crop.right + pad),
                bottom=min(1.0, detection_crop.bottom + pad),
            )
        else:
            target = full_image
    else:
        target = full_image

    data, media_type = encode_image(
        target,
        fmt=upload_crop.format,
        quality=upload_crop.jpeg_quality,
        max_dim=upload_crop.max_dim,
        optimize=upload_crop.jpeg_optimize,
    )
    return data, media_type, used_fallback


def to_normalized_gray(image: np.ndarray, size: tuple[int, int] = (256, 256)) -> np.ndarray:
    """Convert BGR image to a fixed-size float32 grayscale array."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    resized = cv2.resize(gray, size, interpolation=cv2.INTER_AREA)
    return resized.astype(np.float32)


def compute_phash(image: np.ndarray) -> int:
    """Compute a 64-bit perceptual hash (DCT-based pHash) of an image."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    resized = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(resized)
    low_freq = dct[:8, :8]
    # Exclude DC component (0, 0) when computing median
    ac = low_freq.flatten()[1:]
    median = float(np.median(ac))
    bits = (low_freq.flatten() > median).astype(np.uint8)
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value


def hamming_distance(h1: int, h2: int) -> int:
    """Return Hamming distance between two 64-bit hashes."""
    return (h1 ^ h2).bit_count()


def compute_ssim(gray_a: np.ndarray, gray_b: np.ndarray) -> float:
    """Compute Structural Similarity Index (SSIM) between two float32 grayscale images."""
    if gray_a.shape != gray_b.shape:
        gray_b = cv2.resize(
            gray_b, (gray_a.shape[1], gray_a.shape[0]), interpolation=cv2.INTER_AREA
        )

    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2

    mu1 = cv2.GaussianBlur(gray_a, (11, 11), 1.5)
    mu2 = cv2.GaussianBlur(gray_b, (11, 11), 1.5)

    mu1_sq = mu1 * mu1
    mu2_sq = mu2 * mu2
    mu1_mu2 = mu1 * mu2

    sigma1_sq = cv2.GaussianBlur(gray_a * gray_a, (11, 11), 1.5) - mu1_sq
    sigma2_sq = cv2.GaussianBlur(gray_b * gray_b, (11, 11), 1.5) - mu2_sq
    sigma12 = cv2.GaussianBlur(gray_a * gray_b, (11, 11), 1.5) - mu1_mu2

    numerator = (2.0 * mu1_mu2 + c1) * (2.0 * sigma12 + c2)
    denominator = (mu1_sq + mu2_sq + c1) * (sigma1_sq + sigma2_sq + c2)

    ssim_map = numerator / np.maximum(denominator, 1e-12)
    score = float(np.mean(ssim_map))
    return max(0.0, min(1.0, score))


def compute_changed_fraction(
    gray_a: np.ndarray,
    gray_b: np.ndarray,
    *,
    pixel_delta: float = 15.0,
) -> float:
    """Return the fraction of pixels whose intensity changed by more than pixel_delta."""
    if gray_a.shape != gray_b.shape:
        gray_b = cv2.resize(
            gray_b, (gray_a.shape[1], gray_a.shape[0]), interpolation=cv2.INTER_AREA
        )
    diff = np.abs(gray_a - gray_b)
    return float(np.mean(diff > pixel_delta))


def frames_similar(
    phash_a: int,
    phash_b: int,
    gray_a: np.ndarray,
    gray_b: np.ndarray,
    *,
    phash_threshold: int = 4,
    ssim_threshold: float = 0.985,
    max_changed_fraction: float = 0.008,
) -> bool:
    """Return True if two frames are visually similar within configured thresholds."""
    if hamming_distance(phash_a, phash_b) > phash_threshold:
        return False
    if compute_changed_fraction(gray_a, gray_b) > max_changed_fraction:
        return False
    return compute_ssim(gray_a, gray_b) >= ssim_threshold
