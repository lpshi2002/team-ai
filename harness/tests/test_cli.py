import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class CliInitTest(unittest.TestCase):
    def test_init_project_wires_shared_packs(self):
        from team_ai.cli import main

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "team-ai" / "harness").mkdir(parents=True)
            (workspace / "team-ai" / "shared" / "packs").mkdir(parents=True)
            old = Path.cwd()
            try:
                os.chdir(workspace)
                self.assertEqual(main(["init", "project", "alpha-project"]), 0)
            finally:
                os.chdir(old)

            project = workspace / "alpha-project"
            text = (project / ".team-ai" / "project.yaml").read_text(encoding="utf-8")
            self.assertIn("project_id: alpha-project", text)
            self.assertIn("path: ../team-ai/shared/packs/harness-team-ai", text)
            self.assertTrue((project / "src" / "alpha_project" / "__init__.py").exists())
            self.assertTrue((project / ".team-ai" / "sources" / "meetings").is_dir())
            self.assertIn("team-ai context --current-state", (project / "AGENTS.md").read_text(encoding="utf-8"))

    def test_context_cli_forwards_explicit_current_state_view(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest import mock

        from team_ai.context import main

        packet = {
            "refresh": {"snapshot_id": "test"},
            "mode": "normal",
            "intent": "current",
            "retrieval_view": "current_state",
        }
        with mock.patch("team_ai.context.build_context", return_value=packet) as build, redirect_stdout(StringIO()):
            self.assertEqual(main(["--current-state", "Where", "do", "we", "stand?"]), 0)

        build.assert_called_once_with(
            ".",
            "Where do we stand?",
            limit=8,
            mode="auto",
            current_state=True,
        )

    def test_init_project_keeps_default_idea_pack(self):
        from team_ai.cli import main

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "team-ai" / "harness").mkdir(parents=True)
            pack = workspace / "team-ai" / "shared" / "packs" / "finance-ai-ideation"
            pack.mkdir(parents=True)
            (pack / "pack.yaml").write_text("scope: program:finance-ai-ideation\n", encoding="utf-8")
            old = Path.cwd()
            try:
                os.chdir(workspace)
                self.assertEqual(main(["init", "project", "alpha-project"]), 0)
            finally:
                os.chdir(old)

            project = workspace / "alpha-project"
            project_yaml = (project / ".team-ai" / "project.yaml").read_text(encoding="utf-8")
            state_yaml = (project / ".team-ai" / "state.yaml").read_text(encoding="utf-8")
            self.assertIn("scope: program:finance-ai-ideation", project_yaml)
            self.assertIn("path: ../team-ai/shared/packs/finance-ai-ideation", project_yaml)
            self.assertIn("seed_context:", state_yaml)

    def test_init_project_can_run_from_renamed_coworker_repo(self):
        from team_ai.cli import main

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            coworker = workspace / "ai-coworker"
            (coworker / "harness").mkdir(parents=True)
            (coworker / "shared" / "packs").mkdir(parents=True)
            old = Path.cwd()
            try:
                os.chdir(coworker)
                self.assertEqual(main(["init", "project", "alpha-project"]), 0)
            finally:
                os.chdir(old)

            project = workspace / "alpha-project"
            text = (project / ".team-ai" / "project.yaml").read_text(encoding="utf-8")
            self.assertIn("path: ../ai-coworker/shared/packs/harness-team-ai", text)
            self.assertTrue((project / "src" / "alpha_project" / "__init__.py").exists())

    def test_init_project_seeds_selected_idea(self):
        from team_ai.cli import main

        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            pack = workspace / "team-ai" / "shared" / "packs" / "finance-ai-ideation"
            (workspace / "team-ai" / "harness").mkdir(parents=True)
            pack.mkdir(parents=True)
            (pack / "pack.yaml").write_text("scope: program:finance-ai-ideation\n", encoding="utf-8")
            old = Path.cwd()
            try:
                os.chdir(workspace)
                self.assertEqual(
                    main(
                        [
                            "init",
                            "project",
                            "fraud-signal-lab",
                            "--idea",
                            "financial fraud detection and prevention",
                        ]
                    ),
                    0,
                )
            finally:
                os.chdir(old)

            project = workspace / "fraud-signal-lab"
            project_yaml = (project / ".team-ai" / "project.yaml").read_text(encoding="utf-8")
            state_yaml = (project / ".team-ai" / "state.yaml").read_text(encoding="utf-8")
            decision = (project / ".team-ai" / "decisions" / "DEC-INIT-0001.md").read_text(encoding="utf-8")
            normal_block = project_yaml.split("  normal:", 1)[1].split("  implementation:", 1)[0]

            self.assertNotIn("harness:team-ai", normal_block)
            self.assertIn("selected_direction:", state_yaml)
            self.assertIn("financial fraud detection and prevention", state_yaml)
            self.assertIn("financial fraud detection and prevention", decision)
            self.assertIn(".team-ai/compiled/", (project / ".gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
