"""Regenerate the desktop icon assets using the committed MPI master image."""
from pathlib import Path

from PIL import Image, ImageOps


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    assets = root / "frontend" / "build-assets"
    with Image.open(root / "MPI icon.png") as source:
        image = source.convert("RGBA")
    icon = ImageOps.pad(image, (512, 512), method=Image.Resampling.LANCZOS, color=(0, 0, 0, 0))
    icon.save(assets / "icon.png")
    icon.save(assets / "icon-preview.png")
    icon.save(assets / "icon.ico", sizes=[(n, n) for n in (16, 32, 48, 64, 128, 256)])


if __name__ == "__main__":
    main()
