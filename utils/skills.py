import re
import frontmatter
from pathlib import Path

from openai import pydantic_function_tool
from pydantic import BaseModel, Field

from init import BASEDIR

SKILLS_DIR = Path(BASEDIR) / "skills"


class LoadSkill(BaseModel):
    """Load a specialized skill module by name."""
    name: str = Field(
        ...,
        description="The exact name of the skill to load. Must be one of the skills returned by ListSkills."
    )


class ListSkills(BaseModel):
    """Return all available skills with their names and descriptions."""


class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self.skills = {}
        self._load_all()

    def _load_all(self):
        if not self.skills_dir.exists():
            return
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(text)
            name = meta.get("name", f.parent.name)
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple:
        """Parse YAML frontmatter using python-frontmatter."""
        try:
            post = frontmatter.loads(text)
            return post.metadata, post.content
        except Exception as e:
            print(f"Warning: Failed to parse frontmatter: {e}")
            return {}, text

    def get_descriptions(self) -> str:
        """Layer 1: short descriptions for the system prompt."""
        if not self.skills:
            return "(no skills available)"
        lines = []
        for name, skill in self.skills.items():
            desc = skill["meta"].get("description", "No description")
            tags = skill["meta"].get("tags", "")
            line = f"  **{name}**: {desc}"
            if tags:
                line += f" [{tags}]"
            lines.append(line)
        return "\n".join(lines)

    def list_skills(self) -> str:
        """List all available skill names."""
        return f"Skills available: \n{self.get_descriptions()}"

    def get_content(self, name: str) -> str:
        """Layer 2: full skill body returned in tool_result."""
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"


SKILL_LOADER = SkillLoader(SKILLS_DIR)

TOOLS = [
    pydantic_function_tool(LoadSkill),
    pydantic_function_tool(ListSkills),
]

SKILL_NAMESPACE = {
    "type": "namespace",
    "name": "Skills",
    "description": (
        "Tools for discovering and loading specialized skill modules. "
        "Use these tools when a task requires domain-specific knowledge, multi-step reasoning, "
        "or structured problem solving. "
        "Always call 'ListSkills' before calling 'LoadSkill' unless the exact skill name has already been confirmed. "
        "Only load a skill when it is relevant to the user's request."
    ),
    "tools": TOOLS,
}

SKILL_TOOLS = [
    SKILL_NAMESPACE,
]

SKILL_TOOLS_HANDLERS = {
    "LoadSkill": SKILL_LOADER.get_content,
    "ListSkills": SKILL_LOADER.list_skills,
}
