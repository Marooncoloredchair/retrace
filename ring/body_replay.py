"""Still image and a short animation of one pocket Find."""

from __future__ import annotations

import io
import math


def render_replay(track: list[dict], pin: tuple[float, float] | None, name: str) -> tuple[bytes, bytes]:
    from PIL import Image, ImageDraw

    frames = _frames(track)
    images = [_draw(frame, pin, name, index, len(frames)) for index, frame in enumerate(frames, start=1)]
    png = io.BytesIO()
    images[-1].save(png, format="PNG")
    gif = io.BytesIO()
    images[0].save(
        gif,
        format="GIF",
        save_all=True,
        append_images=images[1:],
        duration=120,
        loop=0,
        optimize=True,
    )
    return png.getvalue(), gif.getvalue()


def _frames(track: list[dict]) -> list[list[dict]]:
    if not track:
        return [[]]
    step = max(1, len(track) // 24)
    frames = []
    for end in range(1, len(track) + 1, step):
        frames.append(track[:end])
    if frames[-1] is not track:
        frames.append(track)
    return frames


def _bounds(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), max(xs), min(ys), max(ys)


def _draw(track: list[dict], pin: tuple[float, float] | None, name: str, index: int, total: int) -> object:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 420), (16, 20, 24))
    draw = ImageDraw.Draw(image)
    points = [(float(row["x"]), float(row["y"])) for row in track]
    if pin is not None:
        points.append(pin)
    if not points:
        points = [(0.0, 0.0)]
    min_x, max_x, min_y, max_y = _bounds(points)
    pad = 0.8
    min_x -= pad
    max_x += pad
    min_y -= pad
    max_y += pad
    span_x = max(max_x - min_x, 0.5)
    span_y = max(max_y - min_y, 0.5)
    scale = min(580 / span_x, 340 / span_y)

    def xy(east: float, north: float) -> tuple[float, float]:
        return 30 + (east - min_x) * scale, 360 - (north - min_y) * scale

    if len(track) >= 2:
        draw.line([xy(float(row["x"]), float(row["y"])) for row in track], fill=(154, 165, 176), width=3)
    if pin is not None:
        px, py = xy(*pin)
        draw.ellipse((px - 6, py - 6, px + 6, py + 6), fill=(224, 104, 94))
        draw.text((px + 8, py - 14), name, fill=(224, 104, 94))
        if track:
            draw.line([xy(float(track[-1]["x"]), float(track[-1]["y"])), (px, py)], fill=(224, 178, 74), width=2)
    if track:
        last = track[-1]
        hx, hy = xy(float(last["x"]), float(last["y"]))
        draw.ellipse((hx - 7, hy - 7, hx + 7, hy + 7), fill=(90, 169, 240))
        heading = last.get("heading")
        if heading is not None:
            radians = math.radians(float(heading))
            draw.line(
                [(hx, hy), (hx + math.sin(radians) * 22, hy - math.cos(radians) * 22)],
                fill=(90, 169, 240),
                width=3,
            )
        distance = last.get("distance_m")
        bearing = last.get("bearing")
        if distance is not None and bearing is not None:
            draw.text((16, 16), f"{float(distance):.1f} m   bearing {float(bearing):.0f}°", fill=(224, 178, 74))
    draw.text((16, 392), f"{index} / {total}", fill=(120, 130, 140))
    return image
