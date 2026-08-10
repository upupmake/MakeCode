import json
from pathlib import Path

from pydantic import Field

from utils.tool_validation import ToolArgumentsModel, build_tool_definitions
from rich.markup import escape

from init import log_error_traceback
from system.tui_app import TuiRegion, post_tui
from utils import paths
from utils.skill_catalog import (
    discover_skills,
    read_disabled_skill_names,
    skill_directories,
    write_disabled_skill_names,
)


def _skills_dirs() -> list[Path]:
    return skill_directories()


def print_formatted_text(value):
    post_tui(TuiRegion.STATUS, str(value))

DEFAULT_SKILLS_PROMPT_ENABLED = True


def get_skill_system_note(skill_dir: str, meta_json: str) -> str:
    """Generate the system note for skill loading, providing workspace context."""
    return (
        f"> **[SYSTEM NOTE]**\n"
        f"> The absolute workspace path for this skill is: `{skill_dir}`\n"
        f"> Whenever you need to execute commands, read files, or access any directories (e.g., `scripts/`, `example/`, `output/`) mentioned in this skill document, "
        f"> you MUST resolve them relative to this absolute path (e.g., `{skill_dir}/<relative_path>`).\n\n"
        f"**Skill Metadata:**\n```json\n{meta_json}\n```\n\n"
    )


class LoadSkill(ToolArgumentsModel):
    """
    Load a specialized skill module by name to get its full instructions and context.

    WHEN TO USE:
    - When a task requires domain-specific knowledge or methodology
    - When the system prompt lists available skills relevant to the current task

    WORKFLOW:
    1. Check system prompt's "Skills Catalog" section for available skills
    2. Call LoadSkill with the exact skill name
    3. Follow the returned instructions to complete the task

    RETURNS: Full skill content including instructions, metadata, and file paths.
    """

    name: str = Field(
        ...,
        description="Exact skill name (case-sensitive). Available skills are listed in the system prompt under 'Skills Catalog'.",
    )


class SkillLoader:
    def __init__(
        self,
        skills_dir: Path | None = None,
        disabled_skills_file: Path | None = None,
    ):
        self.skills_dirs = [skills_dir] if skills_dir else _skills_dirs()
        if disabled_skills_file is not None:
            self.disabled_skills_file = disabled_skills_file
        elif skills_dir is not None:
            self.disabled_skills_file = skills_dir / "disabled_skills.json"
        else:
            self.disabled_skills_file = paths.workspace_disabled_skills_file()
        self.all_skills = {}
        self.skills = {}
        self.disabled_skill_names: set[str] = set()
        self.is_enabled = DEFAULT_SKILLS_PROMPT_ENABLED
        self._load_all()

    def refresh_workspace(self) -> None:
        self.skills_dirs = _skills_dirs()
        self.disabled_skills_file = paths.workspace_disabled_skills_file()
        self._load_all()

    def toggle(self) -> str:
        self.is_enabled = not self.is_enabled
        return "skills已加载" if self.is_enabled else "skills已关闭"

    def _load_all(self):
        def report_parse_error(skill_file: Path, exc: Exception) -> None:
            print_formatted_text(
                f"[yellow]Warning: Failed to parse frontmatter in {escape(str(skill_file))}: "
                f"{escape(str(exc))}[/yellow]"
            )

        self.all_skills = discover_skills(self.skills_dirs, report_parse_error)
        try:
            self.disabled_skill_names = read_disabled_skill_names(self.disabled_skills_file)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            self.disabled_skill_names = set()
            print_formatted_text(
                f"[yellow]Warning: Failed to read disabled Skills config "
                f"{escape(str(self.disabled_skills_file))}: {escape(str(exc))}[/yellow]"
            )
        self.skills = {
            name: skill
            for name, skill in self.all_skills.items()
            if name not in self.disabled_skill_names
        }

    def get_skill_entries(self) -> list[dict]:
        self._load_all()
        return [
            {
                "name": name,
                "description": str(skill["meta"]["description"]).strip(),
                "enabled": name not in self.disabled_skill_names,
                "path": skill["path"],
            }
            for name, skill in self.all_skills.items()
        ]

    def set_skill_enabled(self, name: str, enabled: bool) -> None:
        self.apply_skill_enabled_states({name: enabled})

    def apply_skill_enabled_states(self, states: dict[str, bool]) -> None:
        self._load_all()
        unknown_names = set(states) - set(self.all_skills)
        if unknown_names:
            raise ValueError(f"Unknown skill '{sorted(unknown_names)[0]}'")
        disabled_names = set(self.disabled_skill_names)
        for name, enabled in states.items():
            if enabled:
                disabled_names.discard(name)
            else:
                disabled_names.add(name)
        if disabled_names == self.disabled_skill_names:
            return
        write_disabled_skill_names(disabled_names, self.disabled_skills_file)
        self._load_all()

    def get_descriptions(self) -> str:
        """Short descriptions for UI/system prompt injection."""
        self._load_all()
        if not self.skills:
            return "(no skills available)"

        lines = []
        for i, (name, skill) in enumerate(self.skills.items(), 1):
            meta = skill["meta"]
            desc = str(meta.get("description", "No description provided.")).strip()
            desc = desc.replace("\n", " ").replace("\r", "")
            tags = meta.get("tags", "")
            tags_text = ""
            if isinstance(tags, list):
                tags_text = ", ".join(str(tag).strip() for tag in tags if str(tag).strip())
            elif tags:
                tags_text = str(tags).strip()

            line = f"{i}. **{name}**"
            if tags_text:
                line += f" [{tags_text}]"
            line += f"\n   - Description: {desc}"
            line += f"\n   - Directory: {Path(skill['path']).parent.absolute().as_posix()}"
            lines.append(line)
        return "\n".join(lines)

    def render_prompt_block(self) -> str:
        """Render a system-prompt block describing currently available skills."""
        if not self.is_enabled:
            return (
                "\n\n# Skills Catalog Status\n"
                "- Status: OFF\n"
                "- Skills catalog injection into the system prompt is currently disabled.\n"
                "- You may still use `LoadSkill` if the exact skill name is already known from prior context."
            )

        self._load_all()
        skills_paths = "\n".join(
            f"  - `{skills_dir.absolute().as_posix()}`" for skills_dir in self.skills_dirs
        )
        default_install_path = paths.workspace_skills_dir().absolute().as_posix()
        installation_hint = (
            "- To install one skill, move the complete directory containing its `SKILL.md`; for a skill group, "
            "move the complete collection directory, according to the user's requested scope.\n"
        )
        if not self.skills:
            return (
                "\n\n# Skills Catalog Status\n"
                "- Status: ON\n"
                f"- Source directories (highest priority first):\n{skills_paths}\n"
                f"- Default installation directory for user skills: `{default_install_path}`\n"
                f"{installation_hint}"
                "- No skills are currently available in this workspace."
            )

        return (
            "\n\n# Skills Catalog Status\n"
            "- Status: ON\n"
            f"- Source directories (highest priority first):\n{skills_paths}\n"
            f"- Default installation directory for user skills: `{default_install_path}`\n"
            f"{installation_hint}"
            "- The following skills are preloaded into context. When relevant, call `LoadSkill` directly using the exact skill name below.\n\n"
            "## Available Skills\n"
            f"{self.get_descriptions()}"
        )

    def get_content(self, name: str) -> str:
        """Return the full skill body in tool_result."""
        self._load_all()
        skill = self.skills.get(name)
        if not skill:
            return (
                f"Error: Unknown or disabled skill '{name}'. "
                f"Available enabled skills: {', '.join(self.skills.keys())}"
            )

        skill_dir = Path(skill["path"]).parent.absolute().as_posix()
        meta_json = json.dumps(skill["meta"], ensure_ascii=False, indent=2)
        system_note = get_skill_system_note(skill_dir, meta_json)
        return f'<skill name="{name}">\n{system_note}{skill["body"]}\n</skill>'


SKILL_LOADER = SkillLoader()

TOOLS, SKILL_TOOL_MODELS = build_tool_definitions(LoadSkill)

SKILL_NAMESPACE = {
    "type": "namespace",
    "name": "Skills",
    "description": (
        "Tool for loading specialized skill modules by exact name. "
        "Available skills are injected into the system prompt when the skills catalog toggle is on. "
        "Only load a skill when it is relevant to the user's request."
    ),
    "tools": TOOLS,
}

SKILL_TOOLS = [
    SKILL_NAMESPACE,
]

SKILL_TOOLS_HANDLERS = {
    "LoadSkill": SKILL_LOADER.get_content,
}
