"""Offline tests for the task's place in the model key — no server needed.

The suite's other files skip without a live NDIF; these exercise pure string
and config plumbing, so they always run.
"""

from pathlib import Path

from ndif.cli.lib._common import normalize_specs
from ndif.cli.lib.model_config import load_model_config
from ndif.cli.lib.models import (
    extract_repo_id_from_model_key,
    extract_task_from_model_key,
)

_KEY = (
    "nnsight.modeling.transformers.TransformersModel:"
    '{"repo_id": "openai-community/gpt2", "revision": null, "task": "text-generation"}'
)


class TestTaskParsing:
    def test_task_from_a_transformers_key(self):
        assert extract_task_from_model_key(_KEY) == "text-generation"

    def test_none_for_a_non_json_suffix(self):
        # A VLLM key's suffix is the bare repo id.
        assert extract_task_from_model_key("nnsight.modeling.vllm.vllm.VLLM:x/y") is None

    def test_none_for_garbage(self):
        assert extract_task_from_model_key("") is None
        assert extract_task_from_model_key("no-colon-here") is None

    def test_repo_id_extraction_unbothered_by_task(self):
        assert extract_repo_id_from_model_key(_KEY) == "openai-community/gpt2"


class TestSpecPlumbing:
    def test_normalize_specs_carries_task(self):
        specs = normalize_specs([{"checkpoint": "gpt2", "task": "fill-mask"}])
        assert specs[0]["task"] == "fill-mask"

    def test_normalize_specs_defaults_task_to_none(self):
        specs = normalize_specs([{"checkpoint": "gpt2"}])
        assert specs[0]["task"] is None

    def test_model_config_task_field(self, tmp_path: Path):
        config = tmp_path / "models.yaml"
        config.write_text(
            "models:\n"
            "  - gpt2\n"
            "  - checkpoint: openai-community/gpt2\n"
            "    task: feature-extraction\n"
        )
        plain, tasked = load_model_config(config)
        assert plain["task"] is None
        assert tasked["task"] == "feature-extraction"

    def test_model_config_task_default(self, tmp_path: Path):
        config = tmp_path / "models.yaml"
        config.write_text("models:\n  - gpt2\n")
        (spec,) = load_model_config(config, default_task="text-classification")
        assert spec["task"] == "text-classification"
