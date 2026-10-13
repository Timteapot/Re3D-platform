"""Validate unchanged sampled inputs through the platform upload image checker.

No database session or network connection is used. Validation is read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.uploads.service import _inspect_image  # noqa: E402
from backend.uploads.settings import UploadSettings  # noqa: E402


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--count", required=True, type=int)
    args = parser.parse_args()
    directory = args.directory.resolve(strict=True)
    source = args.source.resolve(strict=True)
    settings = UploadSettings()
    require(settings.min_images <= args.count <= settings.max_images,
            "Image count must satisfy the platform upload limits.")
    manifest_path = directory / "selection-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_images = sorted(
        (item for item in source.iterdir() if item.is_file() and item.suffix.lower() in {".jpg", ".jpeg"}),
        key=lambda item: item.name,
    )
    images = [item for item in directory.iterdir() if item.suffix.lower() in {".jpg", ".jpeg"}]
    require(len(images) == args.count == manifest["selected_image_count"],
            "Image count differs from the request or manifest.")
    require(len(manifest["files"]) == args.count, "Manifest file count is inconsistent.")
    require(source == Path(manifest["source_path"]).resolve(strict=True),
            "Manifest source directory differs from the requested source.")
    require(manifest["source_image_count"] == len(source_images),
            "Source image count differs from the manifest.")
    require((directory / "SOURCE.md").is_file(), "Dataset source notice is missing.")
    names: set[str] = set()
    hashes: set[str] = set()
    dimensions: set[tuple[int, int]] = set()
    total_bytes = 0
    max_file_bytes = 0
    for index, record in enumerate(manifest["files"]):
        expected_index = round(index * (len(source_images) - 1) / (args.count - 1))
        original = source_images[expected_index]
        image = directory / record["filename"]
        require(image.resolve(strict=True).parent == directory,
                "Manifest image path escapes the destination directory.")
        require(not image.is_symlink() and not original.is_symlink(),
                "Source and destination images must not be symbolic links.")
        require(record["selection_index"] == index + 1, "Selection index is inconsistent.")
        require(record["source_index"] == expected_index + 1, "Sampling index is inconsistent.")
        require(record["filename"] == original.name, "Sampled filename differs from the source.")
        recorded_source = Path(record.get("source_path", source / record["filename"]))
        require(recorded_source.resolve(strict=True) == original,
                "Recorded source file differs from the selected source file.")
        size_bytes = image.stat().st_size
        require(size_bytes == record["size_bytes"] == original.stat().st_size,
                "Source, destination, or manifest file size differs.")
        require(size_bytes <= settings.max_file_bytes, "Image exceeds the platform file size limit.")
        file_hash = digest(image)
        require(file_hash == record["sha256"] == digest(original),
                "Source, destination, or manifest SHA-256 differs.")
        require(file_hash not in hashes, "Duplicate image contents would be rejected by upload.")
        require(record["filename"] not in names, "Manifest contains duplicate filenames.")
        image_format, width, height = _inspect_image(image, max_pixels=settings.max_pixels)
        require(image_format == "JPEG", "Lighthouse input must decode as JPEG.")
        dimensions.add((width, height))
        hashes.add(file_hash)
        names.add(record["filename"])
        total_bytes += size_bytes
        max_file_bytes = max(max_file_bytes, size_bytes)
    require(total_bytes <= settings.max_total_bytes, "Input exceeds the platform total size limit.")
    require(manifest["files"][0]["source_index"] == 1, "First source frame is missing.")
    require(manifest["files"][-1]["source_index"] == len(source_images),
            "Last source frame is missing.")
    print(json.dumps({
        "status": "passed",
        "directory": str(directory),
        "source": str(source),
        "image_count": args.count,
        "dimensions": sorted(f"{width}x{height}" for width, height in dimensions),
        "total_bytes": total_bytes,
        "max_file_bytes": max_file_bytes,
        "manifest_sha256": digest(manifest_path),
        "file_hash_matches": args.count,
        "platform_decode_passes": args.count,
        "checks": ["uniform_selection", "first_last_frames", "unchanged_source_bytes", "no_duplicates",
                   "image_count", "25MiB_file_limit", "1GiB_total_limit", "50M_pixel_limit"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
