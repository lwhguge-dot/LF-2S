"""Display-only image provider. Real ImageGate integration is reserved for S4."""

import io
import math
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont


class ImageProvider(Protocol):
    def render(self, frame_id: int) -> bytes: ...


class SyntheticImages:
    width, height = 960, 540

    def render(self, frame_id):
        image = Image.new("RGB", (self.width, self.height), "#101d27")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=20)
        small = ImageFont.load_default(size=14)
        for x in range(0, self.width, 60):
            draw.line((x, 0, x, self.height), fill="#203542")
        for y in range(0, self.height, 60):
            draw.line((0, y, self.width, y), fill="#203542")
        draw.rounded_rectangle(
            (220, 100, 740, 435), radius=16, outline="#416879", width=2
        )
        draw.ellipse((408, 197, 552, 341), outline="#66ddc5", width=3)
        draw.line((440, 270, 520, 270), fill="#66ddc5", width=2)
        draw.line((480, 230, 480, 310), fill="#66ddc5", width=2)
        cx = 480 + int(160 * math.sin(frame_id / 18))
        draw.rectangle((cx - 16, 365, cx + 16, 397), fill="#d7bc79")
        draw.rectangle((0, 0, 960, 58), fill="#1f3943")
        draw.text((24, 18), "SIMULATION  /  SYNTHETIC FRAME", font=font, fill="#a9eddf")
        draw.text(
            (24, 488),
            f"FRAME {frame_id:06d}    960 x 540    DISPLAY ONLY",
            font=small,
            fill="#c4d6dc",
        )
        draw.text(
            (244, 121),
            "GENERATED TEST FIELD - NOT A CAMERA",
            font=small,
            fill="#8da8b3",
        )
        stream = io.BytesIO()
        image.save(stream, "JPEG", quality=85)
        return stream.getvalue()
