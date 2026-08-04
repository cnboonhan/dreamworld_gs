"""Convert a 3DGS PLY to Isaac Sim's NuRec USDZ, without 3dgrut's CUDA tracers.

Uses the vendored threedgrut export subtree (Apache 2.0, from nv-tlabs/3dgrut):
PLYImporter.load -> AttributesExportAdapter -> NuRecExporter.export. The
official ply_to_usd.py routes through MixtureOfGaussians, which imports the
compiled OptiX tracers; this stays pure Python
(numpy/plyfile/msgpack/torch/usd-core).

Usage:
    python tools/ply_to_isaac.py splat.ply [out.usdz]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".usdz")

    from threedgrut.export import NuRecExporter
    from threedgrut.export.adapter import AttributesExportAdapter
    from threedgrut.export.importers.ply import PLYImporter

    importer = PLYImporter()
    attrs, caps = importer.load(src)
    model = AttributesExportAdapter(attrs, caps, is_preactivation=importer.stores_preactivation)
    # export_cameras needs a training dataset we don't carry through the PLY
    NuRecExporter(export_cameras=False).export(model, dst)
    print(f"wrote {dst} ({dst.stat().st_size / 1e6:.0f}MB)")


if __name__ == "__main__":
    main()
