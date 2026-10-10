from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from backend.jobs import TaskSubmissionSettings


class TaskSubmissionSettingsTests(unittest.TestCase):
    def test_development_defaults_are_bounded(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = TaskSubmissionSettings.from_environment(
                environment="development"
            )

        self.assertEqual(settings.max_pending_jobs, 3)
        self.assertEqual(settings.max_submissions_per_24h, 20)

    def test_production_requires_both_operator_selected_limits(self) -> None:
        with patch.dict(
            os.environ,
            {"RE3D_USER_MAX_PENDING_JOBS": "2"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "RE3D_USER_MAX_SUBMISSIONS_PER_24H is required",
            ):
                TaskSubmissionSettings.from_environment(
                    environment="production"
                )

    def test_restricted_requires_both_operator_selected_limits(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "MAX_PENDING_JOBS is required"):
                TaskSubmissionSettings.from_environment(
                    environment="restricted"
                )

    def test_daily_limit_cannot_be_lower_than_pending_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be at least"):
            TaskSubmissionSettings(
                max_pending_jobs=5,
                max_submissions_per_24h=4,
            )


if __name__ == "__main__":
    unittest.main()
