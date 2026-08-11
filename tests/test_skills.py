import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from utils import paths
from utils.skills import SkillLoader


class SkillLoaderTests(unittest.TestCase):
    @staticmethod
    def _write_skill(directory: Path, name: str, description: str) -> None:
        skill_dir = directory / description
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n{description}\n",
            encoding="utf-8",
        )

    def test_loads_unique_names_using_directory_priority(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            install_dir = root / "install"
            workspace_dir = root / "workspace"
            legacy_dir = root / "legacy"
            self._write_skill(legacy_dir, "shared", "legacy")
            self._write_skill(workspace_dir, "shared", "workspace")
            self._write_skill(install_dir, "shared", "install")
            self._write_skill(legacy_dir, "legacy-only", "legacy-only")

            with patch(
                "utils.skills._skills_dirs",
                return_value=[install_dir, workspace_dir, legacy_dir],
            ):
                loader = SkillLoader()

            self.assertEqual(set(loader.skills), {"shared", "legacy-only"})
            self.assertEqual(loader.skills["shared"]["meta"]["description"], "install")

    def test_workspace_directory_overrides_legacy_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            install_dir = root / "install"
            workspace_dir = root / "workspace"
            legacy_dir = root / "legacy"
            self._write_skill(legacy_dir, "shared", "legacy")
            self._write_skill(workspace_dir, "shared", "workspace")

            with patch(
                "utils.skills._skills_dirs",
                return_value=[install_dir, workspace_dir, legacy_dir],
            ):
                loader = SkillLoader()

            self.assertEqual(loader.skills["shared"]["meta"]["description"], "workspace")

    def test_skips_skills_without_name_or_description(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_dir = Path(temp_dir)
            invalid_documents = {
                "missing-name": "---\ndescription: missing-name\n---\nbody\n",
                "missing-description": "---\nname: missing-description\n---\nbody\n",
                "blank-name": "---\nname: '  '\ndescription: blank-name\n---\nbody\n",
                "blank-description": "---\nname: blank-description\ndescription: '  '\n---\nbody\n",
            }
            for directory, document in invalid_documents.items():
                skill_dir = skills_dir / directory
                skill_dir.mkdir()
                (skill_dir / "SKILL.md").write_text(document, encoding="utf-8")

            loader = SkillLoader(skills_dir)

            self.assertEqual(loader.skills, {})

    def test_prompt_identifies_default_user_skill_installation_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir) / "workspace"

            with patch.object(paths, "_WORKDIR", workspace_root):
                loader = SkillLoader(workspace_root / "empty-skills")
                prompt = loader.render_prompt_block()

            expected = (workspace_root / ".makecode" / "skills").as_posix()
            self.assertIn(
                f"Default installation directory for user skills: `{expected}`",
                prompt,
            )
            self.assertIn(
                "move the complete directory containing its `SKILL.md`",
                prompt,
            )
            self.assertIn("move the complete collection directory", prompt)

    def test_creates_install_and_workspace_skill_directories_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            install_root = root / "install"
            workspace_root = root / "workspace"

            with (
                patch.object(paths, "_INSTALL_DIR", install_root),
                patch.object(paths, "_WORKDIR", workspace_root),
            ):
                install_skills = paths.install_skills_dir()
                workspace_skills = paths.workspace_skills_dir()
                legacy_skills = paths.workspace_legacy_skills_dir()

            self.assertTrue(install_skills.is_dir())
            self.assertTrue(workspace_skills.is_dir())
            self.assertFalse(legacy_skills.exists())
            self.assertEqual(install_skills, install_root / ".makecode" / "skills")
            self.assertEqual(workspace_skills, workspace_root / ".makecode" / "skills")

    def test_uses_install_makecode_directory_for_published_install_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "published"

            with patch.object(paths, "_INSTALL_DIR", install_root):
                install_skills = paths.install_skills_dir()

            self.assertEqual(install_skills, install_root / ".makecode" / "skills")
            self.assertTrue(install_skills.is_dir())

    def test_switching_workspace_does_not_load_previous_legacy_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            install_root = root / "install"
            previous_workspace = root / "previous"
            current_workspace = root / "current"
            self._write_skill(previous_workspace / "skills", "previous-only", "previous-only")
            self._write_skill(current_workspace / "skills", "current-only", "current-only")

            with (
                patch.object(paths, "_INSTALL_DIR", install_root),
                patch.object(paths, "_WORKDIR", previous_workspace),
            ):
                loader = SkillLoader()
                self.assertIn("previous-only", loader.skills)

                paths.set_workdir(current_workspace)
                loader.refresh_workspace()

            self.assertNotIn("previous-only", loader.skills)
            self.assertIn("current-only", loader.skills)

    def test_disabled_skills_are_persisted_and_excluded_from_prompt_and_load(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            config_file = root / "disabled_skills.json"
            self._write_skill(skills_dir, "enabled-skill", "enabled description")
            self._write_skill(skills_dir, "disabled-skill", "disabled description")
            loader = SkillLoader(skills_dir, config_file)

            loader.set_skill_enabled("disabled-skill", False)

            self.assertEqual(
                json.loads(config_file.read_text(encoding="utf-8")),
                ["disabled-skill"],
            )
            self.assertIn("enabled-skill", loader.skills)
            self.assertNotIn("disabled-skill", loader.skills)
            self.assertNotIn("disabled-skill", loader.render_prompt_block())
            self.assertIn("Unknown or disabled skill 'disabled-skill'", loader.get_content("disabled-skill"))

    def test_slash_commands_include_only_enabled_non_reserved_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            config_file = root / "disabled_skills.json"
            self._write_skill(skills_dir, "enabled-skill", "enabled description")
            self._write_skill(skills_dir, "disabled-skill", "disabled description")
            self._write_skill(skills_dir, "help", "reserved description")
            config_file.write_text('["disabled-skill"]', encoding="utf-8")
            loader = SkillLoader(skills_dir, config_file)

            commands = loader.get_slash_commands({"/help"})

            self.assertEqual(commands, {"/enabled-skill": "enabled description"})

            loader.set_skill_enabled("enabled-skill", False)
            self.assertEqual(loader.get_slash_commands({"/help"}), {})

            loader.set_skill_enabled("enabled-skill", True)
            self.assertEqual(
                loader.get_slash_commands({"/help"}),
                {"/enabled-skill": "enabled description"},
            )

    def test_slash_command_expands_skill_content_and_preserves_user_query(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            self._write_skill(skills_dir, "demo-skill", "demo description")
            loader = SkillLoader(skills_dir, root / "disabled_skills.json")

            expanded = loader.expand_slash_command("/demo-skill 处理这个请求")

            self.assertIsNotNone(expanded)
            self.assertTrue(expanded.startswith("<skill name=\"demo-skill\">"))
            self.assertIn("demo description", expanded)
            self.assertTrue(expanded.endswith("</skill>\n\nUser: /demo-skill 处理这个请求"))
            self.assertEqual(
                loader.expand_slash_command("/demo-skill\n处理这个请求"),
                expanded.replace("/demo-skill 处理", "/demo-skill\n处理"),
            )
            self.assertIsNone(loader.expand_slash_command("/demo-skill-other 处理这个请求"))

    def test_reenabling_skill_removes_it_from_disabled_list(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            config_file = root / "disabled_skills.json"
            self._write_skill(skills_dir, "demo", "demo description")
            config_file.write_text('["demo"]', encoding="utf-8")
            loader = SkillLoader(skills_dir, config_file)

            entries = loader.get_skill_entries()
            self.assertEqual(entries[0]["enabled"], False)

            loader.set_skill_enabled("demo", True)

            self.assertEqual(json.loads(config_file.read_text(encoding="utf-8")), [])
            self.assertIn("demo", loader.skills)

    def test_applies_multiple_skill_states_with_one_config_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skills_dir = root / "skills"
            config_file = root / "disabled_skills.json"
            self._write_skill(skills_dir, "alpha", "alpha description")
            self._write_skill(skills_dir, "beta", "beta description")
            config_file.write_text('["beta", "removed-skill"]', encoding="utf-8")
            loader = SkillLoader(skills_dir, config_file)

            with patch("utils.skills.write_disabled_skill_names") as write_disabled:
                loader.apply_skill_enabled_states({"alpha": False, "beta": True})

            write_disabled.assert_called_once_with(
                {"alpha", "removed-skill"}, config_file
            )

    def test_switching_workspace_refreshes_disabled_skill_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            install_root = root / "install"
            previous_workspace = root / "previous"
            current_workspace = root / "current"
            self._write_skill(install_root / ".makecode" / "skills", "shared", "shared")
            (previous_workspace / ".makecode").mkdir(parents=True)
            (previous_workspace / ".makecode" / "disabled_skills.json").write_text(
                '["shared"]', encoding="utf-8"
            )

            with (
                patch.object(paths, "_INSTALL_DIR", install_root),
                patch.object(paths, "_WORKDIR", previous_workspace),
            ):
                loader = SkillLoader()
                self.assertNotIn("shared", loader.skills)

                paths.set_workdir(current_workspace)
                loader.refresh_workspace()

            self.assertIn("shared", loader.skills)


if __name__ == "__main__":
    unittest.main()
