from __future__ import annotations

import io
import tempfile
import unittest
import uuid
from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.models import Base
from backend.db.queue import JobQueue
from backend.uploads import (
    UploadConflictError,
    UploadService,
    UploadSettings,
    UploadTooLargeError,
    UploadValidationError,
)


class UploadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "data"
        database_path = (self.root / "db.sqlite3").as_posix()
        self.engine = create_engine(f"sqlite:///{database_path}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.queue = JobQueue(self.sessions)
        self.service = UploadService(
            self.sessions,
            self.queue,
            data_root=self.data_root,
            settings=UploadSettings(
                max_file_bytes=4096,
                max_total_bytes=20_000,
                max_pixels=1_000_000,
            ),
        )
        self.user_id = uuid.uuid4()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temporary.cleanup()

    def test_idempotent_creation_duplicate_and_size_limit(self) -> None:
        upload, reused = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-001",
        )
        self.assertFalse(reused)
        repeated, reused = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-001",
        )
        self.assertTrue(reused)
        self.assertEqual(repeated["upload_id"], upload["upload_id"])

        payload = self.png_bytes((100, 20, 30))
        current = self.service.add_image(
            upload_id=upload["upload_id"],
            user_id=self.user_id,
            source=io.BytesIO(payload),
            original_name="../camera.png",
        )
        self.assertEqual(current["image_count"], 1)
        self.assertEqual(current["images"][0]["original_name"], "camera.png")
        with self.assertRaises(UploadConflictError):
            self.service.add_image(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                source=io.BytesIO(payload),
                original_name="duplicate.png",
            )
        with self.assertRaises(UploadTooLargeError):
            self.service.add_image(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                source=io.BytesIO(b"x" * 4097),
                original_name="oversized.jpg",
            )
        staging = (
            self.data_root
            / "jobs"
            / str(upload["upload_id"])
            / "input"
            / "staging"
        )
        self.assertEqual(list(staging.iterdir()), [])

    def test_detects_actual_jpeg_and_rejects_unsupported_image_format(self) -> None:
        upload, _ = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-formats",
        )
        jpeg = self.image_bytes((60, 90, 120), "JPEG")
        current = self.service.add_image(
            upload_id=upload["upload_id"],
            user_id=self.user_id,
            source=io.BytesIO(jpeg),
            original_name="claimed-as-png.png",
        )
        self.assertEqual(current["images"][0]["content_type"], "image/jpeg")

        gif = self.image_bytes((20, 40, 60), "GIF")
        with self.assertRaises(UploadValidationError):
            self.service.add_image(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                source=io.BytesIO(gif),
                original_name="unsupported.gif",
            )

    def test_submission_requires_three_untampered_images(self) -> None:
        upload, _ = self.service.create(
            user_id=self.user_id,
            idempotency_token="upload-unit-002",
        )
        for color in ((200, 10, 10), (10, 200, 10), (10, 10, 200)):
            self.service.add_image(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
                source=io.BytesIO(self.png_bytes(color)),
                original_name="camera.png",
            )
        image_path = next(
            (
                self.data_root
                / "jobs"
                / str(upload["upload_id"])
                / "input"
                / "images"
            ).iterdir()
        )
        image_path.write_bytes(b"tampered")
        with self.assertRaises(UploadValidationError):
            self.service.submit(
                upload_id=upload["upload_id"],
                user_id=self.user_id,
            )
        current = self.service.get(
            upload_id=upload["upload_id"],
            user_id=self.user_id,
        )
        self.assertEqual(current["status"], "uploading")

    @staticmethod
    def png_bytes(color: tuple[int, int, int]) -> bytes:
        return UploadServiceTests.image_bytes(color, "PNG")

    @staticmethod
    def image_bytes(color: tuple[int, int, int], image_format: str) -> bytes:
        output = io.BytesIO()
        Image.new("RGB", (64, 48), color).save(output, format=image_format)
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()
