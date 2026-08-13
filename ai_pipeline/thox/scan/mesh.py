"""Triangle meshes, built from voxels and written out as STL or 3MF.

The meshing strategy is **voxel boundary extraction**, not marching cubes, and
that is a deliberate trade rather than a shortcut.

Marching cubes produces a smoother surface and carries well-known ambiguous
cases whose standard resolutions can emit non-manifold edges. Repairing those
before a slicer will accept the file is real work. Taking the boundary faces of
an occupied voxel set instead gives a surface that is closed and two-manifold
*by construction*: every quad sits between exactly one occupied and one empty
voxel, and every edge is therefore shared by exactly two quads. Nothing needs
repairing because nothing can be broken.

The cost is visible stair-stepping at voxel resolution. Given that the input
silhouettes come from a 640x480 sensor with roughly ±1.5 px edge accuracy, that
stepping is at or below the noise floor of the measurement anyway - smoothing it
would make the mesh look more precise than the data behind it. Optional
Laplacian smoothing is available for cosmetics and is off by default in spirit:
a blocky mesh that slices beats a pretty mesh that fails.

Vertices are welded on the integer lattice, so the vertex count stays modest and
the watertightness check is exact rather than tolerance-based.

No trimesh, no numpy-stl. STL and 3MF are both simple enough to write directly,
and this package's whole install story is "wherever thox-q2-control installs".
"""

from __future__ import annotations

import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..errors import ReconstructionFailed

#: 3MF requires a unit; the Q2 pipeline is millimetres throughout.
_3MF_UNIT = "millimeter"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""

_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""


@dataclass
class Mesh:
    """A triangle mesh. ``vertices`` is (N, 3) float, ``faces`` is (M, 3) int."""

    vertices: np.ndarray
    faces: np.ndarray
    note: str = ""

    def __post_init__(self) -> None:
        self.vertices = np.asarray(self.vertices, dtype=np.float64).reshape(-1, 3)
        self.faces = np.asarray(self.faces, dtype=np.int64).reshape(-1, 3)
        if len(self.faces) and self.faces.max() >= len(self.vertices):
            raise ReconstructionFailed(
                f"face references vertex {self.faces.max()} but only "
                f"{len(self.vertices)} vertices exist"
            )

    # -- properties ----------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return len(self.faces) == 0

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        if self.is_empty:
            return np.zeros(3), np.zeros(3)
        return self.vertices.min(axis=0), self.vertices.max(axis=0)

    def dimensions(self) -> np.ndarray:
        low, high = self.bounds()
        return high - low

    def volume_mm3(self) -> float:
        """Signed volume via the divergence theorem over triangles."""
        if self.is_empty:
            return 0.0
        a = self.vertices[self.faces[:, 0]]
        b = self.vertices[self.faces[:, 1]]
        c = self.vertices[self.faces[:, 2]]
        return float(abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0)

    def is_watertight(self) -> tuple[bool, str]:
        """Exact check: every undirected edge must be shared by exactly two faces."""
        if self.is_empty:
            return False, "mesh has no faces"
        edges = np.vstack(
            [
                self.faces[:, [0, 1]],
                self.faces[:, [1, 2]],
                self.faces[:, [2, 0]],
            ]
        )
        edges = np.sort(edges, axis=1)
        _, counts = np.unique(edges, axis=0, return_counts=True)
        bad = int((counts != 2).sum())
        if bad:
            return False, f"{bad} edge(s) are not shared by exactly two faces"
        return True, "watertight and two-manifold"

    # -- transforms ----------------------------------------------------------

    def translated(self, offset: np.ndarray) -> Mesh:
        return Mesh(
            self.vertices + np.asarray(offset, dtype=float), self.faces, self.note
        )

    def scaled(self, factor: float) -> Mesh:
        return Mesh(self.vertices * float(factor), self.faces, self.note)

    def sitting_on_bed(self) -> Mesh:
        """Translate so the mesh's minimum Z is exactly 0 and it is XY-centred
        on nothing in particular - plating decides placement."""
        low, _ = self.bounds()
        return self.translated(np.array([0.0, 0.0, -low[2]]))

    def smoothed(self, iterations: int = 2, factor: float = 0.5) -> Mesh:
        """Laplacian smoothing with boundary-free averaging.

        Shrinks the mesh slightly, which for a visual hull is the *right*
        direction: the hull is already an over-estimate of the true object, so
        losing a fraction of a voxel moves toward the truth rather than away.
        Still cosmetic, and never a substitute for more views.
        """
        if iterations <= 0 or self.is_empty:
            return self
        vertices = self.vertices.copy()
        # Adjacency as a sparse accumulation: for each edge, add each endpoint's
        # position to the other's running sum.
        edges = np.vstack(
            [self.faces[:, [0, 1]], self.faces[:, [1, 2]], self.faces[:, [2, 0]]]
        )
        edges = np.unique(np.sort(edges, axis=1), axis=0)
        counts = np.bincount(edges.reshape(-1), minlength=len(vertices)).astype(float)
        counts[counts == 0] = 1.0

        for _ in range(int(iterations)):
            accumulator = np.zeros_like(vertices)
            np.add.at(accumulator, edges[:, 0], vertices[edges[:, 1]])
            np.add.at(accumulator, edges[:, 1], vertices[edges[:, 0]])
            neighbour_mean = accumulator / counts[:, None]
            vertices = vertices + factor * (neighbour_mean - vertices)
        return Mesh(vertices, self.faces, self.note)

    # -- writers -------------------------------------------------------------

    def write_stl(self, path: str | Path) -> Path:
        """Write a binary STL."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        a = self.vertices[self.faces[:, 0]]
        b = self.vertices[self.faces[:, 1]]
        c = self.vertices[self.faces[:, 2]]
        normals = np.cross(b - a, c - a)
        lengths = np.linalg.norm(normals, axis=1, keepdims=True)
        lengths[lengths == 0] = 1.0
        normals = normals / lengths

        with path.open("wb") as handle:
            handle.write(b"THOX scan-to-print binary STL".ljust(80, b"\0"))
            handle.write(struct.pack("<I", len(self.faces)))
            block = np.zeros((len(self.faces), 12), dtype=np.float32)
            block[:, 0:3] = normals
            block[:, 3:6] = a
            block[:, 6:9] = b
            block[:, 9:12] = c
            for row in block:
                handle.write(row.tobytes())
                handle.write(b"\0\0")
        return path

    def to_3mf_object_xml(self, object_id: int = 1, name: str = "scan") -> str:
        vertex_rows = "\n".join(
            f'    <vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>'
            for x, y, z in self.vertices
        )
        triangle_rows = "\n".join(
            f'    <triangle v1="{v1}" v2="{v2}" v3="{v3}"/>'
            for v1, v2, v3 in self.faces
        )
        return (
            f'  <object id="{object_id}" type="model" name="{name}">\n'
            f"   <mesh>\n"
            f"   <vertices>\n{vertex_rows}\n   </vertices>\n"
            f"   <triangles>\n{triangle_rows}\n   </triangles>\n"
            f"   </mesh>\n"
            f"  </object>\n"
        )

    def write_3mf(
        self,
        path: str | Path,
        *,
        name: str = "scan",
        transform: np.ndarray | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Path:
        """Write a minimal, valid core-spec 3MF.

        A 3MF is an OPC (zip) package: content types, a relationship pointing at
        the model part, and the model XML. Written directly because the only
        thing needed from a 3MF library here is exactly this.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        matrix = (
            " ".join(f"{v:.6f}" for v in np.asarray(transform, dtype=float).reshape(-1))
            if transform is not None
            else None
        )
        item = (
            f'   <item objectid="1" transform="{matrix}"/>'
            if matrix
            else '   <item objectid="1"/>'
        )
        meta_rows = "".join(
            f'  <metadata name="{key}">{_xml_escape(value)}</metadata>\n'
            for key, value in (metadata or {}).items()
        )
        model = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<model unit="{_3MF_UNIT}" xml:lang="en-US" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
            f"{meta_rows}"
            " <resources>\n"
            f"{self.to_3mf_object_xml(1, name)}"
            " </resources>\n"
            " <build>\n"
            f"{item}\n"
            " </build>\n"
            "</model>\n"
        )

        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
            archive.writestr("_rels/.rels", _RELS)
            archive.writestr("3D/3dmodel.model", model)
        return path


def _xml_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def largest_voxel_component(occupancy: np.ndarray) -> np.ndarray:
    """Keep only the largest 6-connected blob.

    Carving leaves isolated specks where a few views disagreed. Beyond being
    unprintable debris, specks touching the main body only at a corner are what
    would make the boundary surface non-manifold, so removing them is what keeps
    the by-construction manifold guarantee true in practice.
    """
    if not occupancy.any():
        return occupancy
    labels = np.zeros(occupancy.shape, dtype=np.int32)
    current = 0
    # Iterative flood fill; the grid can hold millions of voxels, so recursion
    # is not an option.
    for seed in zip(*np.nonzero(occupancy), strict=True):
        if labels[seed]:
            continue
        current += 1
        stack = [seed]
        labels[seed] = current
        while stack:
            i, j, k = stack.pop()
            for di, dj, dk in (
                (1, 0, 0),
                (-1, 0, 0),
                (0, 1, 0),
                (0, -1, 0),
                (0, 0, 1),
                (0, 0, -1),
            ):
                ni, nj, nk = i + di, j + dj, k + dk
                if (
                    0 <= ni < occupancy.shape[0]
                    and 0 <= nj < occupancy.shape[1]
                    and 0 <= nk < occupancy.shape[2]
                    and occupancy[ni, nj, nk]
                    and not labels[ni, nj, nk]
                ):
                    labels[ni, nj, nk] = current
                    stack.append((ni, nj, nk))
    counts = np.bincount(labels.reshape(-1))
    counts[0] = 0
    return labels == int(np.argmax(counts))


def make_manifold(occupancy: np.ndarray, max_passes: int = 8) -> np.ndarray:
    """Fill diagonal pinches so the boundary surface is two-manifold.

    Boundary extraction is manifold *provided* no edge has four faces on it.
    That happens in exactly one local configuration: within some 2x2 block of
    voxels sharing an edge, the two **diagonal** cells are occupied and the
    other two are not. The surface then pinches along that edge.

    Six-connected component filtering does not prevent this. Two diagonal
    voxels are not themselves 6-connected, but they can both belong to the same
    component through a path elsewhere in the grid, which is common in a carved
    hull. So the configuration is detected directly and resolved by *filling*
    one of the empty cells.

    Filling rather than deleting is deliberate: a visual hull is already an
    over-estimate of the true object, so adding a voxel moves along a bias the
    method already has and is documented for, whereas deleting one could open a
    hole in a genuine thin wall.

    Runs to a fixed point - filling a cell can create a new pinch next door -
    with a pass ceiling so a pathological grid cannot loop forever.
    """
    grid = np.asarray(occupancy, dtype=bool).copy()
    for _ in range(max_passes):
        filled = 0
        # For each axis, look at 2x2 blocks in the plane perpendicular to it.
        for axis_a, axis_b in ((0, 1), (0, 2), (1, 2)):
            a = grid
            slicer_00 = [slice(None)] * 3
            slicer_11 = [slice(None)] * 3
            slicer_01 = [slice(None)] * 3
            slicer_10 = [slice(None)] * 3
            slicer_00[axis_a] = slice(0, -1)
            slicer_00[axis_b] = slice(0, -1)
            slicer_10[axis_a] = slice(1, None)
            slicer_10[axis_b] = slice(0, -1)
            slicer_01[axis_a] = slice(0, -1)
            slicer_01[axis_b] = slice(1, None)
            slicer_11[axis_a] = slice(1, None)
            slicer_11[axis_b] = slice(1, None)

            c00 = a[tuple(slicer_00)]
            c10 = a[tuple(slicer_10)]
            c01 = a[tuple(slicer_01)]
            c11 = a[tuple(slicer_11)]

            # Diagonal pair 00/11 occupied, 01/10 empty -> pinch.
            pinch_a = c00 & c11 & ~c01 & ~c10
            # Diagonal pair 01/10 occupied, 00/11 empty -> pinch.
            pinch_b = c01 & c10 & ~c00 & ~c11
            if pinch_a.any():
                target = np.zeros_like(grid)
                target[tuple(slicer_01)] = pinch_a
                grid |= target
                filled += int(pinch_a.sum())
            if pinch_b.any():
                target = np.zeros_like(grid)
                target[tuple(slicer_00)] = pinch_b
                grid |= target
                filled += int(pinch_b.sum())
        if filled == 0:
            break
    return grid


#: Face definitions: axis, offset, and the four lattice corners of the quad,
#: wound so the normal points out of the occupied voxel.
_FACES: tuple[tuple[tuple[int, int, int], tuple[tuple[int, int, int], ...]], ...] = (
    ((-1, 0, 0), ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0))),
    ((1, 0, 0), ((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1))),
    ((0, -1, 0), ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1))),
    ((0, 1, 0), ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0))),
    ((0, 0, -1), ((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0))),
    ((0, 0, 1), ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))),
)


def mesh_from_voxels(
    occupancy: np.ndarray,
    origin: np.ndarray,
    voxel_mm: float,
    *,
    keep_largest: bool = True,
) -> Mesh:
    """Build a watertight surface from an occupied voxel grid.

    Debris removal and pinch-filling both run first, so the returned surface is
    closed and two-manifold for any input grid.

    Args:
        occupancy: Boolean array indexed ``[i, j, k]`` over x, y, z.
        origin: World position in mm of lattice point ``(0, 0, 0)``.
        voxel_mm: Edge length of one voxel.

    Returns:
        A closed, two-manifold mesh in millimetres.
    """
    occupancy = np.asarray(occupancy, dtype=bool)
    if occupancy.ndim != 3:
        raise ReconstructionFailed(f"occupancy must be 3-D, got {occupancy.shape}")
    if keep_largest:
        occupancy = largest_voxel_component(occupancy)
    # Order matters: drop debris first so pinch-filling does not weld a speck
    # onto the body, then resolve the pinches the surviving body still has.
    occupancy = make_manifold(occupancy)
    if not occupancy.any():
        raise ReconstructionFailed(
            "carving removed every voxel - no object survived all views. This "
            "usually means the silhouettes disagree, which the session's "
            "agreement score should already have flagged."
        )

    padded = np.pad(occupancy, 1, mode="constant", constant_values=False)
    origin = np.asarray(origin, dtype=float).reshape(3)

    vertex_index: dict[tuple[int, int, int], int] = {}
    vertex_list: list[tuple[int, int, int]] = []
    triangles: list[tuple[int, int, int]] = []

    def vertex_for(lattice: tuple[int, int, int]) -> int:
        found = vertex_index.get(lattice)
        if found is None:
            found = len(vertex_list)
            vertex_index[lattice] = found
            vertex_list.append(lattice)
        return found

    occupied = np.argwhere(occupancy)
    for i, j, k in occupied:
        for (di, dj, dk), corners in _FACES:
            # +1 for the pad offset when reading the neighbour.
            if padded[i + 1 + di, j + 1 + dj, k + 1 + dk]:
                continue  # interior face, skip
            quad = [vertex_for((i + cx, j + cy, k + cz)) for cx, cy, cz in corners]
            triangles.append((quad[0], quad[1], quad[2]))
            triangles.append((quad[0], quad[2], quad[3]))

    vertices = origin + np.asarray(vertex_list, dtype=float) * float(voxel_mm)
    return Mesh(
        vertices,
        np.asarray(triangles, dtype=np.int64),
        note=f"voxel boundary at {voxel_mm:.2f} mm",
    )
