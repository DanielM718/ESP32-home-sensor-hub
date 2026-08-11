"""Typed deny-by-default skill registry."""

from butters.skills.implementations import build_read_only_registry
from butters.skills.registry import SkillRegistry

__all__ = ["SkillRegistry", "build_read_only_registry"]
