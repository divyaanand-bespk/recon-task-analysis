import importlib.util
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "generate_pilot_analysis", ROOT / "generate_pilot_analysis.py"
)
GENERATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(GENERATOR)


class FrameParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.srcdoc = None

    def handle_starttag(self, tag, attrs):
        if tag == "iframe":
            self.srcdoc = dict(attrs).get("data-srcdoc")


def sample_row():
    return {
        "name": "sample-task",
        "task_id": "4372a4af-85b5-40a5-a715-193f750486c2",
        "shape": "Migration",
        "ai_rubrics": "Pass",
        "ai_count": 7,
        "pass6": 0,
        "pass6_denominator": 6,
        "eligible_rollouts": 8,
        "incomplete_rollouts": 1,
        "argus_main": "Pass",
        "leading_rp": 24,
        "rp_complete_step": 24,
        "total_rp": 30,
        "tool_calls": 42,
        "fit": "YES",
    }


class GeneratorTests(unittest.TestCase):
    def test_fit_uses_rp_complete_step(self):
        row = sample_row()
        row["leading_rp"] = 100
        row["rp_complete_step"] = 20
        self.assertEqual(GENERATOR.fit_for_pilot(row), "NO")
        row["leading_rp"] = 0
        row["rp_complete_step"] = 21
        self.assertEqual(GENERATOR.fit_for_pilot(row), "YES")
        row["rp_complete_step"] = None
        self.assertEqual(GENERATOR.fit_for_pilot(row), "NO")
        row["leading_rp"] = 21
        self.assertEqual(GENERATOR.fit_for_pilot(row), "YES")

    def test_shape_and_global_review_names(self):
        self.assertTrue(GENERATOR.is_global_review("Grader Coverage"))
        self.assertTrue(GENERATOR.is_global_review("[Blocking] Grader coverage"))
        self.assertTrue(
            GENERATOR.ai_review_passes(
                {
                    "rubric_name": "[Blocking] Grader coverage",
                    "status": "completed",
                    "result": "Fail",
                }
            )
        )
        self.assertEqual(GENERATOR.shape_from_reviews(7, []), "Migration")
        self.assertEqual(GENERATOR.shape_from_reviews(11, []), "Diagnosis")
        self.assertEqual(GENERATOR.shape_from_reviews(13, []), "Optimization")

    def test_argus_low_severity_findings_pass(self):
        review = {
            "status": "completed",
            "result": "fail",
            "finding_severities": ["info", "warning"],
        }
        self.assertEqual(GENERATOR.classify_argus(review), "Pass")
        review["finding_severities"].append("error")
        self.assertEqual(GENERATOR.classify_argus(review), "Fail")

    def test_normalizes_task_link(self):
        value = "https://horizon.bespokelabs.ai/tasks/4372a4af-85b5-40a5-a715-193f750486c2?tab=rubrics"
        self.assertEqual(
            GENERATOR.normalize_task_id(value),
            "4372a4af-85b5-40a5-a715-193f750486c2",
        )

    def test_rp_metrics(self):
        data = {
            "trajectories": [
                {
                    "actions": [
                        {
                            "ordinal": 1,
                            "annotations": {
                                "research_planning": {
                                    "research": True,
                                    "planning": False,
                                }
                            },
                            "arguments": {"keystrokes": "rg --files\n"},
                        },
                        {
                            "ordinal": 2,
                            "annotations": {
                                "research_planning": {
                                    "research": True,
                                    "planning": True,
                                }
                            },
                            "arguments": {
                                "keystrokes": "python3 -c \"open('/app/RESEARCH_AND_PLANNING.md','w').write('done')\"\n"
                            },
                        },
                        {
                            "ordinal": 3,
                            "annotations": {
                                "research_planning": {
                                    "research": False,
                                    "planning": False,
                                }
                            },
                            "arguments": {"keystrokes": "pytest\n"},
                        },
                    ]
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trajectory.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(
                GENERATOR.rp_metrics(path),
                {
                    "leading_rp": 2,
                    "total_rp": 2,
                    "tool_calls": 3,
                    "rp_complete_step": 2,
                },
            )

    def test_rp_metrics_ignores_false_and_unexecuted_writes(self):
        data = {
            "trajectories": [
                {
                    "actions": [
                        {
                            "ordinal": 1,
                            "arguments": {
                                "keystrokes": "open('/app/RESEARCH_AND_PLANNING.md', 'w').write('done')\n"
                            },
                            "observation": "New Terminal Output: done",
                        },
                        {
                            "ordinal": 2,
                            "arguments": {
                                "keystrokes": "head RESEARCH_AND_PLANNING.md\npython3 -c \"open('diagnosis.json')\"\n"
                            },
                            "observation": "Current Terminal Screen: plan contents",
                        },
                        {
                            "ordinal": 3,
                            "arguments": {
                                "keystrokes": "open('/app/RESEARCH_AND_PLANNING.md', 'w').write('later')\n"
                            },
                            "observation": "Previous response had parsing errors: ERROR: Invalid JSON",
                        },
                        {
                            "ordinal": 4,
                            "arguments": {
                                "keystrokes": "p = pathlib.Path('/app/RESEARCH_AND_PLANNING.md'); p.write_text('final')\n"
                            },
                            "observation": "New Terminal Output: done",
                        },
                    ]
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trajectory.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(GENERATOR.rp_metrics(path)["rp_complete_step"], 4)

    def test_render_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.html"
            GENERATOR.write_outputs([sample_row()], output)
            parser = FrameParser()
            outer = output.read_text(encoding="utf-8")
            parser.feed(outer)
            self.assertIn('sandbox="allow-scripts"', outer)
            self.assertIn("default-src 'none'", outer)
            self.assertIsNotNone(parser.srcdoc)
            self.assertNotIn("<script src=", parser.srcdoc)
            data = json.loads(output.with_suffix(".json").read_text())
            self.assertEqual(data["tasks"], [sample_row()])
            self.assertEqual(GENERATOR.load_existing_rows(output), [sample_row()])


if __name__ == "__main__":
    unittest.main()
