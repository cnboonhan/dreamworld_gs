"""A stand-in for decord on platforms it does not ship for.

decord publishes linux wheels for x86_64 only, has no sdist on PyPI, and
the eva-decord fork's arm64 wheels are macOS. Building it from source
means FFmpeg and CMake for a single function: HY-World imports it in
exactly one place, worldgen/src/general_utils.get_last_video_frame, to
read a video's final frame by negative index. Everything else there,
load_video included, already goes through OpenCV.

So this provides the three names that file imports, on top of the OpenCV
the project already depends on. It implements only what is used --
cpu(), indexing (negative included), len(), and .asnumpy() returning RGB
-- and is installed ONLY where real decord cannot be, so x86 keeps the
upstream package.

Frame counts from container metadata are not always truthful, so a seek
that lands nowhere falls back to reading forward, which is slower but
correct.
"""

import cv2
import numpy as np


class _Context:
    def __init__(self, device_id: int = 0):
        self.device_id = device_id


def cpu(device_id: int = 0) -> _Context:
    return _Context(device_id)


def gpu(device_id: int = 0) -> _Context:
    # decord would decode on the GPU here; OpenCV decodes on the CPU and
    # the caller only ever reads pixels back, so the distinction is moot
    return _Context(device_id)


class _Frame:
    """decord hands back a frame object, not an array; .asnumpy() unwraps it."""

    __slots__ = ("_array",)

    def __init__(self, array: np.ndarray):
        self._array = array

    def asnumpy(self) -> np.ndarray:
        return self._array

    def __array__(self, dtype=None):
        return self._array if dtype is None else self._array.astype(dtype)

    @property
    def shape(self):
        return self._array.shape


class VideoReader:
    def __init__(self, uri, ctx=None, width=-1, height=-1, **kwargs):
        self._uri = str(uri)
        self._cap = cv2.VideoCapture(self._uri)
        if not self._cap.isOpened():
            raise RuntimeError(f"cannot open video: {self._uri}")
        count = self._cap.get(cv2.CAP_PROP_FRAME_COUNT)
        self._len = int(count) if count and count > 0 else 0

    def __len__(self) -> int:
        if self._len <= 0:
            self._len = self._count_by_scan()
        return self._len

    def _count_by_scan(self) -> int:
        cap = cv2.VideoCapture(self._uri)
        n = 0
        while cap.grab():
            n += 1
        cap.release()
        return n

    def _read_by_scan(self, index):
        """Read forward. index None means 'the last frame there is'."""
        cap = cv2.VideoCapture(self._uri)
        frame = None
        i = 0
        while True:
            ok, buf = cap.read()
            if not ok:
                break
            if index is None:
                frame = buf
            elif i == index:
                frame = buf
                break
            i += 1
        cap.release()
        if frame is None:
            raise IndexError(f"no frame {index} in {self._uri}")
        return frame

    def __getitem__(self, index: int) -> _Frame:
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index < 0:
            n = self._len
            if n <= 0:
                # unknown length: reading to the end is cheaper than
                # counting first and then seeking
                return _Frame(self._to_rgb(self._read_by_scan(None)))
            index += n
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = self._cap.read()
        if not ok:
            frame = self._read_by_scan(index)
        return _Frame(self._to_rgb(frame))

    def get_batch(self, indices):
        return np.stack([self[i].asnumpy() for i in indices])

    @staticmethod
    def _to_rgb(frame: np.ndarray) -> np.ndarray:
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def __del__(self):
        try:
            self._cap.release()
        except Exception:
            pass


__all__ = ["VideoReader", "cpu", "gpu"]
