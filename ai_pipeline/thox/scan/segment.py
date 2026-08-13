"""Bed-plane segmentation for scan-to-print. numpy + Pillow only.

Ported from the ``thox-q2-vision-scan`` lane. The scan path calls these
functions directly rather than through the print-health provider protocol: the
two vision problems are genuinely different. Print health asks "is this print
failing"; a scan asks "which pixels are the object". Sharing one protocol
between them would make both vaguer.

Differencing against an empty-bed reference is the most direct measurement of
object extent available on this rig - deterministic, ~150 ms, no network, no
cold start. The standalone path exists for when no reference was captured and is
reported at lower confidence, because on a textured PEI sheet the bed's own
speckle competes with the object's edges.
"""
from __future__ import annotations

import io
import time

import numpy as np
from PIL import Image, ImageFilter


#: Minimum fraction of the frame an accepted object must cover. Below this we
#: are almost certainly tracking sensor noise or a speck of debris.
MIN_AREA_FRACTION = 0.0015

#: Maximum fraction. Above this the "object" is probably a lighting change that
#: shifted the whole frame, which differencing cannot distinguish from a very
#: large object - so it is refused rather than reported as a giant part.
MAX_AREA_FRACTION = 0.75


def decode_gray(jpeg: bytes) -> np.ndarray:
    """Decode JPEG bytes to a float32 luminance array in 0..255."""
    with Image.open(io.BytesIO(jpeg)) as image:
        return np.asarray(image.convert("L"), dtype=np.float32)


def decode_rgb(jpeg: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(jpeg)) as image:
        return np.asarray(image.convert("RGB"), dtype=np.float32)


def otsu_threshold(values: np.ndarray, bins: int = 256) -> float:
    """Otsu's threshold: the split maximizing between-class variance.

    Chosen over a fixed threshold because chamber lighting varies with the LED
    state and with ambient light through the door, and a constant would need
    retuning per session.
    """
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 0.0
    lo, hi = float(finite.min()), float(finite.max())
    if hi - lo < 1e-6:
        return hi
    histogram, edges = np.histogram(finite, bins=bins, range=(lo, hi))
    histogram = histogram.astype(np.float64)
    total = histogram.sum()
    if total <= 0:
        return hi
    probability = histogram / total
    centres = (edges[:-1] + edges[1:]) / 2.0
    weight_bg = np.cumsum(probability)
    weight_fg = 1.0 - weight_bg
    mean_total = float((probability * centres).sum())
    mean_bg_cum = np.cumsum(probability * centres)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_bg = mean_bg_cum / weight_bg
        mean_fg = (mean_total - mean_bg_cum) / weight_fg
        between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    between = np.nan_to_num(between, nan=-1.0, posinf=-1.0, neginf=-1.0)
    return float(centres[int(np.argmax(between))])


def _morph(mask: np.ndarray, size: int, dilate: bool) -> np.ndarray:
    """Erosion or dilation with a square structuring element of ``size``."""
    if size <= 1:
        return mask
    if size % 2 == 0:
        size += 1  # Pillow rank filters require an odd window
    image = Image.fromarray((mask * 255).astype(np.uint8))
    kernel = ImageFilter.MaxFilter(size) if dilate else ImageFilter.MinFilter(size)
    return np.asarray(image.filter(kernel), dtype=np.uint8) > 127


def opening(mask: np.ndarray, size: int = 3) -> np.ndarray:
    """Erode then dilate: removes speckle without shrinking the object."""
    return _morph(_morph(mask, size, dilate=False), size, dilate=True)


def closing(mask: np.ndarray, size: int = 5) -> np.ndarray:
    """Dilate then erode: fills pinholes without growing the object."""
    return _morph(_morph(mask, size, dilate=True), size, dilate=False)


def largest_component(mask: np.ndarray) -> np.ndarray:
    """Keep only the largest 8-connected component.

    Two-pass union-find rather than a recursive flood fill: a 640x480 frame can
    hold a component long enough to blow Python's recursion limit, and an
    iterative stack version is slower than the label-merge approach.
    """
    if not mask.any():
        return mask
    height, width = mask.shape
    labels = np.zeros((height, width), dtype=np.int32)
    parent: list[int] = [0]

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # Pass 1: provisional labels with equivalence recording.
    for y in range(height):
        row = mask[y]
        for x in np.flatnonzero(row):
            neighbours = []
            if x > 0 and labels[y, x - 1]:
                neighbours.append(labels[y, x - 1])
            if y > 0:
                for dx in (-1, 0, 1):
                    nx = x + dx
                    if 0 <= nx < width and labels[y - 1, nx]:
                        neighbours.append(labels[y - 1, nx])
            if neighbours:
                smallest = min(neighbours)
                labels[y, x] = smallest
                for other in neighbours:
                    union(smallest, other)
            else:
                parent.append(len(parent))
                labels[y, x] = len(parent) - 1

    # Pass 2: resolve equivalences and pick the biggest.
    flat = labels.reshape(-1)
    nonzero = flat > 0
    if not nonzero.any():
        return np.zeros_like(mask)
    resolved = np.zeros_like(flat)
    resolved[nonzero] = [find(int(v)) for v in flat[nonzero]]
    counts = np.bincount(resolved)
    counts[0] = 0
    return (resolved == int(np.argmax(counts))).reshape(mask.shape)


def laplacian_variance(gray: np.ndarray) -> float:
    """Focus measure. Low variance means a blurred or featureless frame."""
    kernel = np.array([[0.0, 1.0, 0.0], [1.0, -4.0, 1.0], [0.0, 1.0, 0.0]])
    padded = np.pad(gray, 1, mode="edge")
    response = np.zeros_like(gray)
    for dy in range(3):
        for dx in range(3):
            weight = kernel[dy, dx]
            if weight:
                response += (
                    weight * padded[dy : dy + gray.shape[0], dx : dx + gray.shape[1]]
                )
    return float(response.var())


def segment_with_reference(
    frame: np.ndarray, reference: np.ndarray
) -> tuple[np.ndarray, float]:
    """Difference against an empty-bed frame. Returns (mask, confidence)."""
    if frame.shape != reference.shape:
        raise ValueError(f"frame {frame.shape} and reference {reference.shape} differ")
    difference = np.abs(frame - reference)
    threshold = max(otsu_threshold(difference), 8.0)  # floor rejects sensor noise
    mask = difference > threshold
    mask = opening(mask, 3)
    mask = closing(mask, 7)
    mask = largest_component(mask)

    # Confidence from separation: how far the object's difference sits above the
    # threshold relative to the background's spread. A crisp object on a stable
    # bed separates cleanly; a lighting drift does not.
    background = difference[~mask]
    foreground = difference[mask]
    if foreground.size == 0 or background.size == 0:
        return mask, 0.0
    spread = float(background.std()) + 1e-6
    separation = (float(foreground.mean()) - float(background.mean())) / spread
    confidence = float(np.clip(separation / 12.0, 0.0, 0.97))
    return mask, confidence


def segment_standalone(gray: np.ndarray) -> tuple[np.ndarray, float]:
    """Segment without a reference frame, using local contrast.

    Materially worse than differencing on a textured PEI sheet, and reported as
    such. Present so a scan still yields something when the operator forgot to
    capture an empty bed.
    """
    blurred = np.asarray(
        Image.fromarray(gray.astype(np.uint8)).filter(ImageFilter.GaussianBlur(9)),
        dtype=np.float32,
    )
    contrast = np.abs(gray - blurred)
    threshold = max(otsu_threshold(contrast), 6.0)
    mask = contrast > threshold
    mask = closing(mask, 9)
    mask = opening(mask, 5)
    mask = largest_component(mask)
    return mask, 0.35 if mask.any() else 0.0
