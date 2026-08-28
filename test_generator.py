import importlib.util
from html.parser import HTMLParser
import json
from pathlib import Path
import re
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
            # The sandbox deliberately carries the popup tokens: without them the
            # frame blocked every Horizon task link in the report. Assert the
            # tokens rather than the exact attribute string, so widening it again
            # for a further reason does not fail a test that is really about
            # allow-same-origin -- which, together with allow-scripts, would let
            # the frame reach out of the sandbox and must never appear here.
            sandbox = re.search(r'<iframe[^>]*\ssandbox="([^"]*)"', outer)
            self.assertIsNotNone(sandbox)
            tokens = set(sandbox.group(1).split())
            self.assertIn("allow-scripts", tokens)
            self.assertIn("allow-popups", tokens)
            self.assertIn("allow-popups-to-escape-sandbox", tokens)
            self.assertNotIn("allow-same-origin", tokens)
            self.assertIn("default-src 'none'", outer)
            self.assertIsNotNone(parser.srcdoc)
            self.assertNotIn("<script src=", parser.srcdoc)
            data = json.loads(output.with_suffix(".json").read_text())
            self.assertEqual(data["tasks"], [sample_row()])
            self.assertEqual(GENERATOR.load_existing_rows(output), [sample_row()])

    def test_csv_keeps_carriage_returns_inside_quoted_fields(self):
        """The parse used to drop 1.8% of every exported trajectory.

        Terminal output is full of CRLF. Splitting the COPY stream with
        str.splitlines() threw the terminator away, so a newline inside a quoted
        field came back as a bare \n and the \r was gone -- silently, with the
        right number of rows, which is why it survived so long. Measured against
        the server: 20 of 20 rollouts corrupted, 80,508 of 4,498,714 characters
        lost. Reading through io.StringIO(text, newline="") is exact.
        """
        content = "line one\r\nline two\r\nline three"
        payload = 'seq,content\n1,"%s"\n' % content
        db = GENERATOR.HorizonDatabase(1)
        db._query = lambda _sql: payload
        rows = db.csv("SELECT 1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], content)

    def test_balanced_chunks_covers_everything_and_respects_the_cap(self):
        values = list(range(43))
        chunks = GENERATOR.balanced_chunks(values, jobs=12, cap=24)
        self.assertEqual([v for chunk in chunks for v in chunk], values)
        self.assertTrue(all(len(chunk) <= 24 for chunk in chunks))
        # The invariant that matters: no worker is handed more than its share,
        # so batching cannot starve the pool the way a fixed size did.
        self.assertLessEqual(max(len(chunk) for chunk in chunks), -(-43 // 12))
        # 24 rollouts over 12 workers is 12 chunks, not the 2 a fixed size of 12
        # produced -- that regression is what made batching slower than not.
        self.assertEqual(len(GENERATOR.balanced_chunks(list(range(24)), 12, 24)), 12)
        # The cap is a statement-timeout ceiling and is never exceeded.
        wide = GENERATOR.balanced_chunks(list(range(1000)), jobs=2, cap=24)
        self.assertTrue(all(len(chunk) <= 24 for chunk in wide))
        self.assertEqual(GENERATOR.balanced_chunks([], jobs=12, cap=24), [])

    def test_rp_cache_is_keyed_on_the_rollout_not_the_task_name(self):
        rollout = "2f950392-6bd1-40fc-8ca3-73bc5d4441fe"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "runs" / "stamp"
            task = run / "tasks" / "some-task"
            task.mkdir(parents=True)
            (task / "trajectory-001.jsonl").write_text(
                json.dumps({"rollout_id": rollout, "sequence_number": 1,
                            "role": "user", "content": "hi"}) + "\n",
                encoding="utf-8")
            output = run / "outputs" / "some-task"
            output.mkdir(parents=True)
            (output / "annotated_trajectories.json").write_text(
                json.dumps({"trajectories": [{"actions": []}]}), encoding="utf-8")
            cache = root / "cache"
            self.assertEqual(
                GENERATOR.seed_rp_cache_from_runs(root / "runs", cache), 1)
            self.assertTrue(GENERATOR.rp_cache_path(cache, rollout).is_file())
            # Idempotent: a second pass adopts nothing and overwrites nothing.
            self.assertEqual(
                GENERATOR.seed_rp_cache_from_runs(root / "runs", cache), 0)


if __name__ == "__main__":
    unittest.main()
