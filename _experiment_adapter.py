"""Versioned experiment registry assembly for the thin AstrBot adapter."""

import os

from dududa.core.experiments import ExperimentRegistry, default_experiment_specs


def make_experiment_registry(plugin_data_dir: str) -> ExperimentRegistry:
    """Build the shared Bot/Control-Plane registry from deployment settings."""
    path = os.environ.get(
        "DUDUDA_EXPERIMENT_FILE",
        os.path.join(plugin_data_dir, "data", "experiments.json"),
    )
    return ExperimentRegistry(
        path=path,
        bucket_salt=os.environ.get("DUDUDA_EXPERIMENT_BUCKET_SALT", ""),
        defaults=default_experiment_specs(),
    )
