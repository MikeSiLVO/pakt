"""Print each runtime dependency floor in pyproject.toml as an exact pin, for pip-audit."""

import re
import tomllib
from pathlib import Path

project = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())["project"]
extras = project.get("optional-dependencies", {})
for spec in project["dependencies"] + [s for name, deps in extras.items() if name != "dev" for s in deps]:
    match = re.match(r"([A-Za-z0-9_.-]+)\s*>=\s*([^,;\s]+)", spec)
    if match:
        print(f"{match.group(1)}=={match.group(2)}")
