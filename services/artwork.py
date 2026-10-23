from pathlib import Path

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3


def apply_playlist_artwork(files: list[Path], image: bytes) -> tuple[int, list[str]]:
    updated = 0
    failures: list[str] = []
    mime = "image/png" if image.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
    for path in files:
        try:
            if path.suffix.lower() == ".flac":
                audio = FLAC(path)
                picture = Picture()
                picture.type = 3
                picture.mime = mime
                picture.desc = "Cover"
                picture.data = image
                audio.clear_pictures()
                audio.add_picture(picture)
                audio.save()
            elif path.suffix.lower() == ".mp3":
                tags = ID3(path)
                tags.delall("APIC")
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image))
                tags.save(path)
            else:
                continue
            updated += 1
        except Exception as exc:
            failures.append(f"{path.name}: {exc}")
    return updated, failures
