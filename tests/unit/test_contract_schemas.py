from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = ROOT / "packages" / "contracts" / "schemas" / "v1"
EXAMPLE_ROOT = ROOT / "packages" / "contracts" / "examples" / "v1" / "valid"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def validator(name: str) -> Draft202012Validator:
    schema = load_json(SCHEMA_ROOT / f"{name}.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def assert_invalid(
    test: unittest.TestCase,
    contract_validator: Draft202012Validator,
    instance: dict[str, Any],
) -> None:
    errors = list(contract_validator.iter_errors(instance))
    test.assertTrue(errors, "mutated contract unexpectedly passed validation")


class ContractSchemaTests(unittest.TestCase):
    def test_all_schemas_are_valid_draft_2020_12(self) -> None:
        schema_paths = sorted(SCHEMA_ROOT.glob("*.schema.json"))
        self.assertEqual(len(schema_paths), 4)
        for schema_path in schema_paths:
            with self.subTest(schema=schema_path.name):
                Draft202012Validator.check_schema(load_json(schema_path))

    def test_valid_pipeline_request(self) -> None:
        validator("pipeline-request").validate(
            load_json(EXAMPLE_ROOT / "pipeline-request.json")
        )

    def test_pipeline_request_rejects_branch_or_path_override(self) -> None:
        request = load_json(EXAMPLE_ROOT / "pipeline-request.json")
        request_validator = validator("pipeline-request")

        missing_branch = copy.deepcopy(request)
        missing_branch["branches"] = ["A-v4", "B-v2"]
        assert_invalid(self, request_validator, missing_branch)

        absolute_path = copy.deepcopy(request)
        absolute_path["input"]["images_path"] = "D:/private/images"
        assert_invalid(self, request_validator, absolute_path)

    def test_valid_pipeline_events_and_sequence(self) -> None:
        events = load_jsonl(EXAMPLE_ROOT / "pipeline-events.jsonl")
        event_validator = validator("pipeline-event")
        for event in events:
            event_validator.validate(event)

        self.assertEqual(
            [event["sequence"] for event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(len({event["event_id"] for event in events}), len(events))
        self.assertEqual(len({event["job_id"] for event in events}), 1)

    def test_pipeline_event_rejects_missing_progress_and_absolute_path(self) -> None:
        events = load_jsonl(EXAMPLE_ROOT / "pipeline-events.jsonl")
        event_validator = validator("pipeline-event")

        missing_progress = copy.deepcopy(events[2])
        del missing_progress["progress"]
        assert_invalid(self, event_validator, missing_progress)

        absolute_path = copy.deepcopy(events[3])
        absolute_path["artifact"]["path"] = "D:/private/mesh.glb"
        assert_invalid(self, event_validator, absolute_path)

    def test_valid_pipeline_result(self) -> None:
        validator("pipeline-result").validate(
            load_json(EXAMPLE_ROOT / "pipeline-result.json")
        )

    def test_pipeline_result_enforces_terminal_semantics(self) -> None:
        result = load_json(EXAMPLE_ROOT / "pipeline-result.json")
        result_validator = validator("pipeline-result")

        failed_without_error = copy.deepcopy(result)
        failed_without_error["status"] = "failed"
        assert_invalid(self, result_validator, failed_without_error)

        succeeded_with_failed_branch = copy.deepcopy(result)
        succeeded_with_failed_branch["branches"]["A-v4"]["status"] = "failed"
        assert_invalid(self, result_validator, succeeded_with_failed_branch)

    def test_valid_evaluation(self) -> None:
        validator("evaluation").validate(load_json(EXAMPLE_ROOT / "evaluation.json"))

    def test_evaluation_rejects_invalid_score_and_path_traversal(self) -> None:
        evaluation = load_json(EXAMPLE_ROOT / "evaluation.json")
        evaluation_validator = validator("evaluation")

        invalid_score = copy.deepcopy(evaluation)
        invalid_score["overall"]["score"] = 101
        assert_invalid(self, evaluation_validator, invalid_score)

        path_traversal = copy.deepcopy(evaluation)
        path_traversal["shared_dimensions"]["input"]["checks"][0][
            "evidence_refs"
        ] = ["../private/input.json"]
        assert_invalid(self, evaluation_validator, path_traversal)

    def test_examples_describe_the_same_job_and_request(self) -> None:
        request = load_json(EXAMPLE_ROOT / "pipeline-request.json")
        events = load_jsonl(EXAMPLE_ROOT / "pipeline-events.jsonl")
        result = load_json(EXAMPLE_ROOT / "pipeline-result.json")
        evaluation = load_json(EXAMPLE_ROOT / "evaluation.json")

        self.assertTrue(all(event["job_id"] == request["job_id"] for event in events))
        self.assertEqual(result["job_id"], request["job_id"])
        self.assertEqual(result["request_id"], request["request_id"])
        self.assertEqual(evaluation["job_id"], request["job_id"])


if __name__ == "__main__":
    unittest.main()
