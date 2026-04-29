#!/usr/bin/env python3
"""Resolve recipes.config.yaml into the AgentCore build context.

Reads ``${RECIPES_CONFIG:-recipes.config.yaml}``. For each enabled
recipe: locates the stock directory (``recipes/<name>/`` or an explicit
``path:`` outside the repo), deep-merges per-recipe ``overrides:`` into
the stock ``recipe.yaml``, validates every required secret exists in
AWS Secrets Manager, copies the recipe directory into
``app/hermes/recipes/<name>/`` (drops ``setup.sh`` — local-only), and
emits ``app/hermes/recipes_manifest.json``.

Exits non-zero on any error so ``deploy.sh`` fails fast.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", os.getcwd())).resolve()
CONFIG_NAME = os.environ.get("RECIPES_CONFIG", "recipes.config.yaml")
CONFIG_PATH = (PROJECT_DIR / CONFIG_NAME).resolve()

OUTPUT_DIR = PROJECT_DIR / "app" / "hermes" / "recipes"
MANIFEST_PATH = PROJECT_DIR / "app" / "hermes" / "recipes_manifest.json"


def _info(msg: str) -> None:
    print(f"[INFO]   {msg}")


def _error(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def _deep_merge(base, override):
    """Dicts merge per-key recursively; lists and scalars in override replace."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for key, value in override.items():
            out[key] = _deep_merge(out[key], value) if key in out else value
        return out
    return override


def _load_yaml(path: Path) -> dict:
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _secret_exists(secret_id: str) -> bool:
    """Use AWS CLI rather than boto3 — no Python AWS deps in deploy host."""
    return subprocess.run(
        ["aws", "secretsmanager", "describe-secret", "--secret-id", secret_id],
        capture_output=True,
    ).returncode == 0


def _resolve_recipe(entry: dict, idx: int) -> tuple[str, Path, dict]:
    name = entry.get("name")
    if not name:
        raise SystemExit(f"recipes[{idx}]: missing 'name'")

    if entry.get("path"):
        source_dir = (PROJECT_DIR / entry["path"]).resolve()
    else:
        source_dir = (PROJECT_DIR / "recipes" / name).resolve()

    if not source_dir.is_dir():
        raise SystemExit(
            f"recipe '{name}': directory not found at {source_dir} "
            f"(set 'path:' for recipes outside this repo)"
        )

    recipe_yaml = source_dir / "recipe.yaml"
    if not recipe_yaml.exists():
        raise SystemExit(f"recipe '{name}': missing recipe.yaml at {recipe_yaml}")

    stock = _load_yaml(recipe_yaml)
    overrides = entry.get("overrides") or {}
    return name, source_dir, _deep_merge(stock, overrides)


def _normalize_prompt_list(raw) -> list[str]:
    """Accept a string, a list of strings, or None — return a list of strings."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(item) for item in raw if item]
    raise SystemExit(f"system_prompt must be a string or list, got {type(raw).__name__}")


def _emit_empty_manifest() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(
            {"secrets": {}, "mcp_servers": {}, "system_prompts": []},
            indent=2,
        )
    )


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Ensure a fresh build context — no stale recipe directories from a
    # prior deploy with different recipes enabled.
    for child in OUTPUT_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child)

    if not CONFIG_PATH.exists():
        _info(f"No recipes config at {CONFIG_NAME} — recipes disabled.")
        _emit_empty_manifest()
        return

    config = _load_yaml(CONFIG_PATH)
    recipes = config.get("recipes") or []
    if not recipes:
        _info(f"{CONFIG_NAME} has no enabled recipes.")
        _emit_empty_manifest()
        return

    manifest: dict = {"secrets": {}, "mcp_servers": {}, "system_prompts": []}

    for idx, entry in enumerate(recipes):
        name, source_dir, merged = _resolve_recipe(entry, idx)
        suffix = " (overrides applied)" if entry.get("overrides") else ""
        _info(f"Enabling recipe: {name}{suffix}")

        for secret_id in (merged.get("secrets") or {}):
            if not _secret_exists(secret_id):
                raise SystemExit(
                    f"recipe '{name}': secret {secret_id!r} not found in "
                    f"Secrets Manager. Run: ./scripts/setup_recipe.sh {name}"
                )

        # Copy stock files into the build context. Drop setup.sh — it's
        # interactive and only useful from a developer machine.
        dest = OUTPUT_DIR / name
        shutil.copytree(
            source_dir, dest,
            ignore=shutil.ignore_patterns("setup.sh", "__pycache__"),
        )
        # Replace the copied recipe.yaml with the post-override version so
        # the build context reflects what's actually deployed (the runtime
        # reads recipes_manifest.json, but having the merged file in place
        # is useful for debugging from inside the container).
        with (dest / "recipe.yaml").open("w") as fh:
            yaml.safe_dump(merged, fh, sort_keys=False)

        manifest["secrets"].update(merged.get("secrets") or {})
        manifest["mcp_servers"].update(merged.get("mcp_servers") or {})
        manifest["system_prompts"].extend(
            _normalize_prompt_list(merged.get("system_prompt"))
        )

    # Deployment-level system_prompt (top-level of recipes.config.yaml) is
    # appended last so it can reference any recipe loaded above.
    manifest["system_prompts"].extend(
        _normalize_prompt_list(config.get("system_prompt"))
    )

    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code and not isinstance(exc.code, int):
            _error(str(exc))
            sys.exit(1)
        raise
    except Exception as exc:
        _error(f"Unexpected error: {exc}")
        sys.exit(1)
