"""The editor's line to the qwen server — panorama restyles over HTTP.

The server is main's, ported verbatim: Qwen-Image-Edit-2509 behind POST
/restyle. Main's editor sent it perspective view-crops for brushed inpaints;
here the WHOLE equirect goes in and seamless=true wrap-blends the 360 seam,
which is the server's own design for full-panorama work. Output comes back
at the model's working size (1536x768 by default), not the capture's — a
variant is a look, and the look is decided at model resolution.
"""

import os

import requests

QWEN = os.environ.get("QWEN_URL", "http://qwen:8000")


def ready() -> bool:
    try:
        return (requests.get(f"{QWEN}/health", timeout=3)
                .json().get("status") == "ok")
    except (requests.RequestException, ValueError):
        return False


def restyle(image_bytes: bytes, prompt: str, width: int = 1536,
            height: int = 768, steps: int = 40, guidance: float = 4.0,
            seed: int = 0) -> bytes:
    r = requests.post(
        f"{QWEN}/restyle",
        files=[("image", ("pano.png", image_bytes, "image/png"))],
        data={"prompt": prompt, "steps": steps, "guidance": guidance,
              "seed": seed, "width": width, "height": height,
              "seamless": "true", "void_fill": "false"},
        timeout=900)
    r.raise_for_status()
    if "image" not in r.headers.get("content-type", ""):
        raise RuntimeError(r.text[:300])
    return r.content
