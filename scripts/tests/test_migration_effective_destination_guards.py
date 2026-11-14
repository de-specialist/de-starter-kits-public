"""Offline tests for effective destination configuration refusals."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "migration"))

try:
    from migration import run_migration
except ModuleNotFoundError as error:
    # Only a missing optional runtime package may skip, and never in CI.
    if os.environ.get("CI", "").casefold() in {"1", "true", "yes"} or error.name not in {"dlt", "sqlalchemy"}:
        raise
    run_migration = None


@unittest.skipIf(run_migration is None, "migration runtime dependencies are not installed")
class EffectiveDestinationGuardTests(unittest.TestCase):
    dataset = "starterkit_migration_guard"

    def _pipeline(
        self,
        destination: str,
        *,
        replace_strategy: str | None = None,
        staging_dataset_name_layout: str = "%s_staging",
        staging: object | None = None,
        credential_query: dict[str, object] | None = None,
        snowflake_host_override: str | None = None,
    ):
        if destination == "bigquery":
            credentials = None
            project_id = "offline-project"
        else:
            credentials = SimpleNamespace(
                host="offline-account",
                database="OFFLINE_DB",
                query=credential_query,
                _snowflake_host=snowflake_host_override,
            )
            project_id = None

        class DestinationConfig:
            pass

        config = DestinationConfig()
        config.replace_strategy = replace_strategy
        config.staging_dataset_name_layout = staging_dataset_name_layout
        config.credentials = credentials
        config.project_id = project_id
        config.normalize_dataset_name = lambda _schema: self.dataset

        client = SimpleNamespace(config=config)

        return SimpleNamespace(
            destination=SimpleNamespace(destination_type=f"dlt.destinations.{destination}"),
            staging=staging,
            config=SimpleNamespace(use_single_dataset=True, dataset_name_layout=None),
            dataset_name=self.dataset,
            pipeline_name="offline_guard_fixture",
            destination_client=lambda: client,
        )

    def _resolve_cloud(self, pipeline, destination: str) -> tuple[object, ...]:
        if destination == "bigquery":
            environment = {"BQ_PROJECT": "offline-project"}
            target_id = "offline-project"
        else:
            environment = {
                "SNOWFLAKE_ACCOUNT": "offline-account",
                "SNOWFLAKE_DATABASE": "OFFLINE_DB",
            }
            target_id = "offline-account/OFFLINE_DB"
        with mock.patch.dict(os.environ, environment, clear=False):
            return run_migration._resolve_dlt_destination(
                pipeline, destination, target_id, self.dataset
            )

    def test_default_and_truncate_replace_strategies_are_accepted_for_cloud(self) -> None:
        for destination in ("bigquery", "snowflake"):
            for strategy in (None, "truncate-and-insert"):
                with self.subTest(destination=destination, strategy=strategy):
                    pipeline = self._pipeline(destination, replace_strategy=strategy)
                    result = self._resolve_cloud(pipeline, destination)
                    self.assertEqual(result[3], self.dataset)

    def test_staging_optimized_replace_strategy_is_refused_for_cloud(self) -> None:
        for destination in ("bigquery", "snowflake"):
            with self.subTest(destination=destination):
                pipeline = self._pipeline(destination, replace_strategy="staging-optimized")
                with self.assertRaisesRegex(
                    ValueError,
                    "Effective dlt replace_strategy is unsupported; only truncate-and-insert replacement is allowed\\.",
                ):
                    self._resolve_cloud(pipeline, destination)

    def test_active_staging_destination_is_refused(self) -> None:
        pipeline = self._pipeline("bigquery", staging=object())
        with self.assertRaisesRegex(
            ValueError, "dlt staging destinations are unsupported by this replacement loader\\."
        ):
            self._resolve_cloud(pipeline, "bigquery")

    def test_custom_staging_dataset_layout_is_refused(self) -> None:
        pipeline = self._pipeline(
            "bigquery", staging_dataset_name_layout="custom_%s_stage"
        )
        with self.assertRaisesRegex(
            ValueError, "Custom dlt staging namespace layouts are unsupported\\."
        ):
            self._resolve_cloud(pipeline, "bigquery")

    def test_snowflake_query_endpoint_overrides_are_refused(self) -> None:
        cases = (
            {"credential_query": {"host": "override.example.invalid"}},
            {"credential_query": {"account": "override-account"}},
            {"snowflake_host_override": "https://override.example.invalid"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                pipeline = self._pipeline("snowflake", **overrides)
                with self.assertRaisesRegex(
                    ValueError, "Custom Snowflake endpoint overrides are unsupported\\."
                ):
                    self._resolve_cloud(pipeline, "snowflake")

    def test_snowflake_warehouse_and_role_query_options_remain_allowed(self) -> None:
        pipeline = self._pipeline(
            "snowflake", credential_query={"warehouse": "OFFLINE_WH", "role": "OFFLINE_ROLE"}
        )
        result = self._resolve_cloud(pipeline, "snowflake")
        self.assertEqual(result[2], f"offline-account/OFFLINE_DB/{self.dataset}")


if __name__ == "__main__":
    unittest.main()
