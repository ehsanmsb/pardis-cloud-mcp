"""Non-secret YAML configuration for selecting the deployment's tools."""
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from pardis_cloud_mcp.deployment import Name


ToolName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ToolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: list[ToolName]
    disabled: list[ToolName] = Field(default_factory=list)


class PlanningConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    allowed_roles: list[Name] = Field(min_length=1, max_length=64)
    plan_ttl_seconds: int = Field(default=1800, ge=60, le=86400)


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tools: ToolConfig
    planning: PlanningConfig | None = None


def load_config(path: str) -> Config:
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot read MCP YAML configuration: {path}") from exc
    return Config.model_validate(document)
