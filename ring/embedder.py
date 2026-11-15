"""DINOv2 ViT-S/14 embeddings for the keyframe bank.

The DINOv2 weights stay unloaded until a place-find embedding is actually
needed. Starting ReTrace, and a PDR test, do not load the model.

Runs on a worker thread. The engine only enqueues JPEG bytes; if the queue is
full the frame is dropped so the capture loop never blocks.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Callable

import numpy as np

MODEL_NAME = "dinov2_vits14"
EMBED_DIM = 384


class Embedder:
    def __init__(
        self,
        on_embedding: Callable[[int, np.ndarray, float], None],
        queue_size: int = 8,
        enabled: bool = True,
    ) -> None:
        self.on_embedding = on_embedding
        self.enabled = enabled
        self.queue: queue.Queue = queue.Queue(maxsize=max(1, queue_size))
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.model = None
        self.device = "cpu"
        self.ready = False
        self.error = ""
        self.enqueued = 0
        self.dropped = 0
        self.done = 0
        self._infer_lock = threading.Lock()
        self._recent: deque[tuple[float, np.ndarray]] = deque(maxlen=64)
        self.hold_load = False

    def start(self) -> None:
        if not self.enabled:
            self.error = "embedder disabled in config"
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="ring-embedder")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        if self.thread is not None:
            self.thread.join(timeout=5)

    def submit(self, keyframe_id: int, jpeg: bytes, host_mono: float) -> bool:
        """False when the queue is full, the embedder is off, or a PDR test is holding the model unloaded."""
        if not self.enabled or self.hold_load:
            return False
        try:
            self.queue.put_nowait((keyframe_id, jpeg, host_mono))
            self.enqueued += 1
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "device": self.device,
            "error": self.error,
            "enqueued": self.enqueued,
            "dropped": self.dropped,
            "done": self.done,
            "queue": self.queue.qsize(),
            "model": MODEL_NAME,
            "dim": EMBED_DIM,
        }

    def _load(self) -> None:
        import torch

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = torch.hub.load("facebookresearch/dinov2", MODEL_NAME, verbose=False)
        self.model.eval()
        self.model.to(self.device)
        if self.device == "cuda":
            self.model.half()
        self.ready = True

    def _embed(self, jpeg: bytes) -> np.ndarray:
        import torch
        import torchvision.transforms as T
        from PIL import Image
        import io

        image = Image.open(io.BytesIO(jpeg)).convert("RGB")
        transform = T.Compose(
            [
                T.Resize(256, interpolation=T.InterpolationMode.BICUBIC),
                T.CenterCrop(224),
                T.ToTensor(),
                T.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
            ]
        )
        tensor = transform(image).unsqueeze(0)
        if self.device == "cuda":
            tensor = tensor.to(self.device).half()
        else:
            tensor = tensor.to(self.device)
        with torch.inference_mode():
            features = self.model(tensor)
        vector = features.float().cpu().numpy().reshape(-1)
        norm = float(np.linalg.norm(vector)) or 1.0
        return (vector / norm).astype(np.float32)

    def embed_bytes(self, jpeg: bytes) -> np.ndarray | None:
        """Synchronous embed for place-find. This is the call that loads DINOv2."""
        if not self.enabled or self.hold_load:
            return None
        if not self.ready:
            try:
                self._load()
            except Exception as error:
                self.error = f"{type(error).__name__}: {error}"
                return None
        try:
            with self._infer_lock:
                return self._embed(jpeg)
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            return None

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.ready or self.hold_load:
                self._stop.wait(0.2)
                continue
            try:
                item = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                return
            keyframe_id, jpeg, host_mono = item
            try:
                with self._infer_lock:
                    embedding = self._embed(jpeg)
                self.done += 1
                self.on_embedding(keyframe_id, embedding, host_mono)
            except Exception as error:
                self.error = f"{type(error).__name__}: {error}"


def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(1.0 - np.dot(a, b) / (float(np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))


def farthest_point_sample(embeddings: list[np.ndarray], count: int) -> list[int]:
    """Indices of the most mutually diverse embeddings (farthest-point sampling)."""
    if not embeddings or count <= 0:
        return []
    if len(embeddings) <= count:
        return list(range(len(embeddings)))
    chosen = [0]
    min_dist = [cosine_distance(embeddings[0], embeddings[i]) for i in range(len(embeddings))]
    while len(chosen) < count:
        nxt = int(np.argmax(min_dist))
        if nxt in chosen:
            # All remaining distances are zero; fill sequentially.
            for index in range(len(embeddings)):
                if index not in chosen:
                    chosen.append(index)
                    break
            continue
        chosen.append(nxt)
        for index, vector in enumerate(embeddings):
            min_dist[index] = min(min_dist[index], cosine_distance(embeddings[nxt], vector))
    return chosen
