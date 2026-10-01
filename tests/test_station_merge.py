import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class StationMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "station.db"),
            RuleEngine(),
        )
        self.station_a = Actor("observer-a", "analyst", "station-a")
        self.station_b = Actor("observer-b", "analyst", "station-b")
        self.admin = Actor("admin-1", "admin", "hq")

    def tearDown(self):
        self.tmp.cleanup()

    def _candidate(self, actor=None, event_id="AT-1"):
        actor = actor or self.station_a
        source = self.service.create(
            actor,
            "source",
            {"name": "Survey", "survey_name": "S"},
        )
        return self.service.create(
            actor,
            "candidate",
            {
                "source_id": source["id"],
                "event_id": event_id,
                "ra": 10,
                "dec": 20,
                "magnitude": 18,
                "transient_type": "unknown",
                "observed_at": "2026-09-27T00:00:00Z",
            },
        )

    def _measurement(self, observed_at, magnitude=18.0, station_id=None, ra=10.0, dec=20.0):
        m = {"observed_at": observed_at, "ra": ra, "dec": dec, "magnitude": magnitude}
        if station_id:
            m["station_id"] = station_id
        return m

    # ---- station scoping ----

    def test_observer_can_only_submit_own_station_measurement(self):
        candidate = self._candidate()
        # own station: ok
        updated = self.service.transition(
            self.station_a,
            candidate["id"],
            "merge_measurement",
            {"measurement": self._measurement("2026-09-27T01:00:00Z", station_id="station-a")},
        )
        self.assertEqual(updated["data"]["merged_measurement_count"], 2)
        # other station: rejected
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.station_a,
                candidate["id"],
                "merge_measurement",
                {"measurement": self._measurement("2026-09-27T02:00:00Z", station_id="station-b")},
            )

    def test_admin_can_submit_any_station(self):
        candidate = self._candidate()
        updated = self.service.transition(
            self.admin,
            candidate["id"],
            "merge_measurement",
            {"measurement": self._measurement("2026-09-27T01:00:00Z", station_id="station-b")},
        )
        self.assertEqual(updated["data"]["merged_measurement_count"], 2)

    # ---- batch merge ----

    def test_batch_merge_merges_valid_and_retains_invalid(self):
        candidate = self._candidate()
        updated = self.service.transition(
            self.station_a,
            candidate["id"],
            "merge_batch",
            {
                "measurements": [
                    self._measurement("2026-09-27T01:00:00Z", magnitude=17.5),
                    self._measurement("2026-09-27T02:00:00Z", magnitude="bad"),
                    self._measurement("2026-09-27T03:00:00Z", magnitude=16.0),
                ]
            },
        )
        result = updated["data"]["batch_result"]
        self.assertEqual(result["merged_count"], 2)
        self.assertEqual(result["rejected_count"], 1)
        self.assertEqual(updated["data"]["pending_count"], 1)
        # priority reflects the latest (brightest) merged measurement
        self.assertEqual(updated["data"]["latest_magnitude"], 16.0)
        self.assertGreater(updated["data"]["priority_score"], 20)

    def test_batch_merge_rejects_duplicate_timestamp(self):
        candidate = self._candidate()
        updated = self.service.transition(
            self.station_a,
            candidate["id"],
            "merge_batch",
            {
                "measurements": [
                    self._measurement("2026-09-27T01:00:00Z"),
                    self._measurement("2026-09-27T01:00:00Z"),
                ]
            },
        )
        result = updated["data"]["batch_result"]
        self.assertEqual(result["merged_count"], 1)
        self.assertEqual(result["rejected_count"], 1)
        self.assertEqual(result["rejected"][0]["code"], "duplicate")

    def test_batch_merge_requires_non_empty_list(self):
        candidate = self._candidate()
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.station_a, candidate["id"], "merge_batch", {"measurements": []}
            )

    # ---- retry pending ----

    def test_retry_pending_keeps_invalid_and_discards_duplicates(self):
        candidate = self._candidate()
        # batch with one valid and one invalid
        updated = self.service.transition(
            self.station_a,
            candidate["id"],
            "merge_batch",
            {
                "measurements": [
                    self._measurement("2026-09-27T01:00:00Z"),
                    self._measurement("2026-09-27T02:00:00Z", magnitude="bad"),
                ]
            },
        )
        self.assertEqual(updated["data"]["pending_count"], 1)
        # retry: invalid stays, attempts increment
        retried = self.service.transition(
            self.station_a, candidate["id"], "retry_pending", {}
        )
        self.assertEqual(retried["data"]["pending_count"], 1)
        self.assertEqual(retried["data"]["pending_measurements"][0]["attempts"], 2)
        # now merge a corrected version of the invalid one
        self.service.transition(
            self.station_a,
            candidate["id"],
            "merge_measurement",
            {"measurement": self._measurement("2026-09-27T02:00:00Z", magnitude=17.0)},
        )
        # retry again: the pending item is now a duplicate -> discarded
        retried = self.service.transition(
            self.station_a, candidate["id"], "retry_pending", {}
        )
        self.assertEqual(retried["data"]["pending_count"], 0)
        self.assertEqual(retried["data"]["retry_result"]["discarded_duplicates"], 1)

    def test_retry_pending_without_pending_is_error(self):
        candidate = self._candidate()
        with self.assertRaises(ValidationError):
            self.service.transition(self.station_a, candidate["id"], "retry_pending", {})

    def test_discard_pending_clears_stale_items(self):
        candidate = self._candidate()
        updated = self.service.transition(
            self.station_a,
            candidate["id"],
            "merge_batch",
            {"measurements": [self._measurement("2026-09-27T01:00:00Z", magnitude="bad")]},
        )
        self.assertEqual(updated["data"]["pending_count"], 1)
        discarded = self.service.transition(
            self.station_a, candidate["id"], "discard_pending", {}
        )
        self.assertEqual(discarded["data"]["pending_count"], 0)

    # ---- concurrency ----

    def test_concurrent_batch_merge_does_not_lose_updates(self):
        candidate = self._candidate()
        # station A reads the candidate (version 1)
        stale = self.service.get(candidate["id"])
        # station A merges first
        first = self.service.transition(
            self.station_a,
            candidate["id"],
            "merge_batch",
            {"measurements": [self._measurement("2026-09-27T01:00:00Z", station_id="station-a")]},
            expected_version=1,
        )
        self.assertEqual(first["version"], 2)
        # station B merges with the stale version -> conflict, not overwrite
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.station_b,
                candidate["id"],
                "merge_batch",
                {"measurements": [self._measurement("2026-09-27T02:00:00Z", station_id="station-b")]},
                expected_version=1,
            )
        # station B retries with the current version -> both measurements present
        retried = self.service.transition(
            self.station_b,
            candidate["id"],
            "merge_batch",
            {"measurements": [self._measurement("2026-09-27T02:00:00Z", station_id="station-b")]},
            expected_version=2,
        )
        self.assertEqual(retried["version"], 3)
        stations = {m["station_id"] for m in retried["data"]["measurements"]}
        self.assertEqual(stations, {"station-a", "station-b"})

    # ---- priority recalculation ----

    def test_recalculate_priority_uses_latest_magnitude(self):
        candidate = self._candidate()
        updated = self.service.transition(
            self.station_a,
            candidate["id"],
            "merge_batch",
            {
                "measurements": [
                    self._measurement("2026-09-27T01:00:00Z", magnitude=16.0),
                    self._measurement("2026-09-27T02:00:00Z", magnitude=19.0),
                ]
            },
        )
        # latest is 19.0 -> lower priority
        self.assertEqual(updated["data"]["latest_magnitude"], 19.0)
        priority_at_19 = updated["data"]["priority_score"]
        # recalculate gives the same deterministic result
        recalculated = self.service.transition(
            self.station_a, candidate["id"], "recalculate_priority", {}
        )
        self.assertEqual(recalculated["data"]["priority_score"], priority_at_19)
        self.assertIn("priority_recalculated_at", recalculated["data"])

    def test_reclassify_recomputes_priority(self):
        candidate = self._candidate()
        updated = self.service.transition(
            self.station_a,
            candidate["id"],
            "merge_measurement",
            {"measurement": self._measurement("2026-09-27T01:00:00Z", magnitude=16.0)},
        )
        self.assertEqual(updated["data"]["transient_type"], "unknown")
        priority_unknown = updated["data"]["priority_score"]
        reclassified = self.service.transition(
            self.station_a,
            candidate["id"],
            "reclassify",
            {"new_type": "grb", "reason": "follow-up spectrum"},
        )
        # grb has a higher type weight -> priority increases
        self.assertGreater(reclassified["data"]["priority_score"], priority_unknown)

    # ---- scheduled observations protection ----

    def test_candidate_with_active_observations_cannot_be_withdrawn(self):
        candidate = self._candidate()
        telescope = self.service.create(
            self.admin,
            "telescope",
            {"name": "Scope", "aperture_m": 2.0, "site_name": "site"},
        )
        observation = self.service.create(
            self.admin,
            "observation",
            {
                "candidate_id": candidate["id"],
                "telescope_id": telescope["id"],
                "team_id": "team-1",
                "start_at": "2026-09-28T10:00:00Z",
                "end_at": "2026-09-28T11:00:00Z",
                "mode": "imaging",
            },
        )
        self.service.transition(self.admin, observation["id"], "schedule", {"operator_id": "op-1"})
        # withdrawing the candidate would orphan the scheduled observation
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin, candidate["id"], "withdraw", {"reason": "false positive"}
            )
        # the scheduled observation is untouched
        fetched = self.service.get(observation["id"])
        self.assertEqual(fetched["status"], "scheduled")

    # ---- batch idempotency ----

    def test_batch_idempotency_returns_stored_result(self):
        candidate = self._candidate()
        batch = {
            "batch_id": "night-1",
            "measurements": [self._measurement("2026-09-27T01:00:00Z")],
        }
        first = self.service.transition(self.station_a, candidate["id"], "merge_batch", batch)
        self.assertEqual(first["data"]["batch_result"]["merged_count"], 1)
        # resubmitting the same batch_id does not merge twice
        second = self.service.transition(self.station_a, candidate["id"], "merge_batch", batch)
        self.assertEqual(second["data"]["batch_result"]["merged_count"], 1)
        self.assertEqual(second["data"]["merged_measurement_count"], 2)


if __name__ == "__main__":
    unittest.main()
