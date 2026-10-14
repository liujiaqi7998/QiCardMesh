"""Session-bound image CAPTCHA used before sending email codes."""

import io
import random
import secrets
import time
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from PIL import Image, ImageDraw, ImageFont


CAPTCHA_SESSION_KEY = "qicardmesh:captcha"
CAPTCHA_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _font(size):
    candidates = (
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    )
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _new_code():
    # Require both classes so the challenge is always alphanumeric.
    while True:
        code = "".join(secrets.choice(CAPTCHA_ALPHABET) for _ in range(5))
        if any(char.isdigit() for char in code) and any(char.isalpha() for char in code):
            return code


def _render(code):
    width, height = 180, 64
    image = Image.new("RGB", (width, height), (246, 249, 255))
    draw = ImageDraw.Draw(image)
    for _ in range(7):
        draw.line(
            [
                (random.randint(0, width), random.randint(0, height)),
                (random.randint(0, width), random.randint(0, height)),
            ],
            fill=(random.randint(120, 205), random.randint(150, 220), random.randint(190, 235)),
            width=1,
        )
    for _ in range(90):
        x, y = random.randrange(width), random.randrange(height)
        draw.point((x, y), fill=(random.randint(80, 180), random.randint(100, 190), random.randint(130, 210)))

    font = _font(38)
    x = 13
    for char in code:
        layer = Image.new("RGBA", (48, 54), (0, 0, 0, 0))
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.text((8, 1), char, font=font,
                        fill=(25, 67, 125, 255), stroke_width=1,
                        stroke_fill=(10, 35, 78, 255))
        layer = layer.rotate(random.randint(-18, 18), resample=Image.Resampling.BICUBIC, expand=1)
        image.paste(layer, (x, random.randint(-4, 8)), layer)
        x += 33

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


@never_cache
@require_GET
def image(request):
    code = _new_code()
    request.session[CAPTCHA_SESSION_KEY] = {
        "code": code.casefold(),
        "expires_at": time.time() + settings.CAPTCHA_TTL,
    }
    request.session.modified = True
    response = HttpResponse(_render(code), content_type="image/png")
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


def verify(request, supplied):
    """Consume the current session challenge and compare case-insensitively."""
    challenge = request.session.pop(CAPTCHA_SESSION_KEY, None)
    request.session.modified = True
    if not isinstance(challenge, dict):
        return False
    expected = challenge.get("code")
    expires_at = challenge.get("expires_at", 0)
    try:
        expired = time.time() > float(expires_at)
    except (TypeError, ValueError):
        expired = True
    if not isinstance(expected, str) or expired:
        return False
    if not isinstance(supplied, str):
        return False
    return secrets.compare_digest(expected, supplied.strip().casefold())
