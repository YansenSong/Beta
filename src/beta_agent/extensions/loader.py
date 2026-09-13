from __future__ import annotations
import importlib.util
from pathlib import Path
from .types import ExtensionError, ExtensionFactory

def load_extensions_from_dir(path: str | Path, *, errors: list[ExtensionError] | None = None) -> list[ExtensionFactory]:
    root = Path(path)
    factories: list[ExtensionFactory] = []
    sink = errors if errors is not None else []
    for file in sorted(root.glob("*.py")):
        if file.name.startswith("_"):
            continue
        try:
            module_name = f"beta_agent_extension_{file.stem}_{abs(hash(file.resolve()))}"
            spec = importlib.util.spec_from_file_location(module_name, file)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load module spec: {file}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            factory = getattr(module, "extension", None)
            if not callable(factory):
                raise TypeError("module must expose callable `extension`")
            factories.append(factory)
        except Exception as exc:
            sink.append(ExtensionError(stage="import", extension=file.name, message=str(exc)))
    return factories
