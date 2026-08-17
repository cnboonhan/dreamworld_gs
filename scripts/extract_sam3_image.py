"""Derive a standalone SAM 3 image model from the SAM 3 video packaging.

HY-World's trajectory planner loads `Sam3Model`/`Sam3Processor`, but the only
ungated distribution of SAM 3 (ModelScope facebook/sam3) ships the *video*
packaging: config with nested detector_config/tracker_config, weights prefixed
detector_model.*/tracker_model.*. This promotes the detector half into a
standalone transformers repo the image classes can load.

Usage:
    python extract_sam3_image.py <sam3_video_dir> <out_dir>
"""

import json
import shutil
import sys
from pathlib import Path

from safetensors import safe_open
from safetensors.torch import save_file

PREFIX = "detector_model."


def main() -> None:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)

    cfg = json.load(open(src / "config.json"))
    det = cfg["detector_config"]
    det["architectures"] = ["Sam3Model"]
    det.setdefault("dtype", cfg.get("dtype", "float32"))
    json.dump(det, open(dst / "config.json", "w"), indent=2)

    tensors = {}
    with safe_open(src / "model.safetensors", framework="pt") as f:
        for k in f.keys():
            if k.startswith(PREFIX):
                tensors[k[len(PREFIX):]] = f.get_tensor(k)
    save_file(tensors, str(dst / "model.safetensors"))

    for name in ("tokenizer.json", "tokenizer_config.json",
                 "special_tokens_map.json", "vocab.json", "merges.txt"):
        if (src / name).exists():
            shutil.copy2(src / name, dst / name)

    # the packaged tokenizer_config asks for the slow CLIPTokenizer, which
    # transformers 5.2 cannot build against tokenizers>=0.23
    tc_path = dst / "tokenizer_config.json"
    if tc_path.exists():
        tc = json.load(open(tc_path))
        tc["tokenizer_class"] = "CLIPTokenizerFast"
        json.dump(tc, open(tc_path, "w"), indent=2)

    json.dump({"image_processor_type": "Sam3ImageProcessorFast",
               "processor_class": "Sam3Processor"},
              open(dst / "preprocessor_config.json", "w"), indent=2)

    print(f"extracted {len(tensors)} tensors -> {dst}")


if __name__ == "__main__":
    main()
