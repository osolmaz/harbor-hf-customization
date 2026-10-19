"""Observe native installed agents without replacing their execution or accounting.

The shared resource owner samples the task environment during native run/cleanup.
Publication follows Harbor's native session conversion: populating metadata sooner
would make Trial skip that conversion. Effort records input and resolved options only.
"""

import json

from anonbench1_adapters.resource_telemetry import (
    begin_resource_observation,
    observe_resources,
    publish_resources,
)

EFFORT_KEY = "reasoning_effort_observation"


class StandaloneObservations:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._requested_effort = {
            "present": "reasoning_effort" in kwargs,
            "value": kwargs.get("reasoning_effort"),
        }

    @observe_resources
    async def run(self, instruction, environment, context):
        self._effort_observation = {
            "source": "harbor.agent_kwargs_and_resolved_options.reasoning_effort",
            "scope": "configuration_not_runtime_observation",
            "requested": self._requested_effort,
            "configured": self.options.reasoning_effort,
            "native_selected": None,
            "wire_effective": None,
            "provider_effective": None,
        }
        begin_resource_observation()
        return await super().run(instruction, environment, context)

    @publish_resources
    def populate_context_post_run(self, context):
        super().populate_context_post_run(context)
        observation = getattr(self, "_effort_observation", None)
        if observation is None:
            return
        context.metadata = {**(context.metadata or {}), EFFORT_KEY: observation}
        trajectory_path = self.logs_dir / "trajectory.json"
        try:
            if trajectory_path.is_file():
                trajectory = json.loads(trajectory_path.read_text())
                metrics = trajectory.get("final_metrics") if isinstance(trajectory, dict) else None
                if isinstance(metrics, dict):
                    metrics["extra"] = {**(metrics.get("extra") or {}), EFFORT_KEY: observation}
                    trajectory_path.write_text(json.dumps(trajectory, indent=2) + "\n")
        except (OSError, ValueError, TypeError) as error:
            self.logger.debug("Effort observation publication failed: %s", type(error).__name__)
