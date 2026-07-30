from pathlib import Path
from typing import Callable

import frontmatter

from utils import paths


def skill_directories(*, create: bool = True) -> list[Path]:
    directories = [
        paths.install_skills_dir(create=create),
        paths.workspace_skills_dir(create=create),
        paths.workspace_legacy_skills_dir(),
    ]
    return list(dict.fromkeys(directories))


def discover_skills(
    directories: list[Path] | None = None,
    on_parse_error: Callable[[Path, Exception], None] | None = None,
) -> dict[str, dict]:
    skills = {}
    source_directories = directories if directories is not None else skill_directories()
    for skills_dir in reversed(source_directories):
        if not skills_dir.exists():
            continue
        for skill_file in sorted(skills_dir.rglob("SKILL.md")):
            text = skill_file.read_text(encoding="utf-8")
            try:
                post = frontmatter.loads(text)
                metadata = post.metadata
                body = post.content
            except Exception as exc:
                if on_parse_error is not None:
                    on_parse_error(skill_file, exc)
                continue
            name = metadata.get("name")
            description = metadata.get("description")
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(description, str) or not description.strip():
                continue
            skills[name.strip()] = {
                "meta": metadata,
                "body": body,
                "path": str(skill_file),
            }
    return skills
