"""Compatibility facade for :mod:`beta_agent.providers.model`."""
from .providers.model import ModelAdapter, ScriptedModelAdapter, accepts_request_options

__all__ = ["ModelAdapter", "ScriptedModelAdapter", "accepts_request_options"]
