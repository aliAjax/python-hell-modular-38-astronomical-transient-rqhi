import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class StationBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "stations.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.analyst = Actor("analyst-1", "analyst")
        self.source = self.service.create(
            self.analyst, "source", {"name": "Survey", "survey_name": "S"}
        )
        self.candidate = self.service.create(
            self.analyst,
            "candidate",
            {
                "source_id": self.source["id"],
                "event_id": "AT-2026-042",
                "ra": 120.5,
                "dec": -12.25,
                "magnitude": 18.0,
                "transient_type": "supernova",
                "observed_at": "2026-09-30T20:00:00Z",
            },
        )
        self.coordinator = Actor("coordinator-1", "coordinator")

    def tearDown(self):
        self.tmp.cleanup()

    def _station(self, name, observers):
        station = self.service.create(
            self.coordinator,
            "station",
            {"name": name, "site_name": "Site " + name, "observer_ids": observers},
        )
        return self.service.transition(self.coordinator, station["id"], "activate", {})

    def _batch(self, actor, station, measurements):
        batch = self.service.create(
            actor,
            "measurement_batch",
            {
                "candidate_id": self.candidate["id"],
                "station_id": station["id"],
                "measurements": measurements,
            },
        )
        return self.service.transition(actor, batch["id"], "submit", {})

    def _measurement(self, observed_at, magnitude, ra=120.5, dec=-12.25):
        return {
            "observed_at": observed_at,
            "ra": ra,
            "dec": dec,
            "magnitude": magnitude,
        }

    def test_offline_batch_merges_all_buffered_measurements(self):
        station = self._station("North", ["obs-north"])
        observer = Actor("obs-north", "operator", station["id"])
        batch = self._batch(
            observer,
            station,
            [
                self._measurement("2026-09-30T22:00:00Z", 17.5),
                self._measurement("2026-09-30T23:00:00Z", 17.1),
                self._measurement("2026-10-01T00:00:00Z", 16.6),
            ],
        )
        self.assertEqual(batch["status"], "completed")
        self.assertEqual(batch["data"]["merged_count"], 3)
        self.assertEqual(batch["data"]["blocked_count"], 0)
        candidate = self.service.get(self.candidate["id"])
        self.assertEqual(candidate["data"]["merged_measurement_count"], 4)
        self.assertEqual(candidate["data"]["latest_magnitude"], 16.6)
        for record in candidate["data"]["measurements"][1:]:
            self.assertEqual(record["station_id"], station["id"])
        self.assertNotIn("measurement", candidate["data"])

    def test_two_stations_same_timestamp_both_merge(self):
        north = self._station("North", ["obs-north"])
        south = self._station("South", ["obs-south"])
        when = "2026-09-30T22:30:00Z"
        north_batch = self._batch(
            Actor("obs-north", "operator", north["id"]),
            north,
            [self._measurement(when, 17.4)],
        )
        south_batch = self._batch(
            Actor("obs-south", "operator", south["id"]),
            south,
            [self._measurement(when, 17.6)],
        )
        self.assertEqual(north_batch["status"], "completed")
        self.assertEqual(south_batch["status"], "completed")
        candidate = self.service.get(self.candidate["id"])
        self.assertEqual(candidate["data"]["merged_measurement_count"], 3)
        stations = {
            record["station_id"]
            for record in candidate["data"]["measurements"]
            if record["observed_at"] == when
        }
        self.assertEqual(stations, {north["id"], south["id"]})

    def test_concurrent_batches_loser_is_retained_and_retriable(self):
        north = self._station("North", ["obs-north"])
        south = self._station("South", ["obs-south"])
        when = "2026-09-30T22:45:00Z"

        first_batch = self.service.create(
            Actor("obs-north", "operator", north["id"]),
            "measurement_batch",
            {
                "candidate_id": self.candidate["id"],
                "station_id": north["id"],
                "measurements": [self._measurement(when, 17.4)],
            },
        )
        # Force the first write ahead so the queued batch starts from a stale
        # candidate version and loses the optimistic-lock race.
        self.service.transition(
            Actor("obs-north", "operator", north["id"]), first_batch["id"], "submit", {}
        )
        loser = self.service.create(
            Actor("obs-south", "operator", south["id"]),
            "measurement_batch",
            {
                "candidate_id": self.candidate["id"],
                "station_id": south["id"],
                "measurements": [self._measurement(when, 17.6)],
            },
        )

        # The real repository sees the up-to-date version; the batch was
        # buffered against the candidate's pre-merge version.
        stale_version = self.candidate["version"]

        class StaleCandidateRepository:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def get_entity(self, entity_id):
                entity = self.inner.get_entity(entity_id)
                if entity and entity_id == loser["data"]["candidate_id"]:
                    # Roll back the version as it was when the station buffered
                    # the batch overnight.
                    entity["version"] = stale_version
                return entity

        service = DomainService(StaleCandidateRepository(self.repo), RuleEngine())
        submitted = service.transition(
            Actor("obs-south", "operator", south["id"]), loser["id"], "submit", {}
        )
        self.assertEqual(submitted["status"], "partial")
        self.assertEqual(submitted["data"]["blocked_count"], 1)
        item = submitted["data"]["measurements"][0]
        self.assertEqual(item["status"], "blocked")
        self.assertTrue(item["error"])

        # The real repository sees the up-to-date version; retry converges.
        retried = self.service.transition(
            Actor("obs-south", "operator", south["id"]), loser["id"], "retry", {}
        )
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(retried["data"]["merged_count"], 1)
        candidate = self.service.get(self.candidate["id"])
        self.assertEqual(candidate["data"]["merged_measurement_count"], 3)

    def test_cross_station_submission_is_rejected(self):
        north = self._station("North", ["obs-north"])
        south = self._station("South", ["obs-south"])
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("obs-south", "operator", north["id"]),
                "measurement_batch",
                {
                    "candidate_id": self.candidate["id"],
                    "station_id": north["id"],
                    "measurements": [self._measurement("2026-09-30T22:00:00Z", 17.0)],
                },
            )
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("obs-north", "operator", south["id"]),
                "measurement_batch",
                {
                    "candidate_id": self.candidate["id"],
                    "station_id": south["id"],
                    "measurements": [self._measurement("2026-09-30T22:00:00Z", 17.0)],
                },
            )

    def test_observer_without_station_header_cannot_submit(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                Actor("obs-north", "operator"),
                "measurement_batch",
                {
                    "candidate_id": self.candidate["id"],
                    "station_id": "any",
                    "measurements": [self._measurement("2026-09-30T22:00:00Z", 17.0)],
                },
            )

    def test_inactive_station_submission_is_rejected(self):
        station = self._station("North", ["obs-north"])
        self.service.transition(
            self.coordinator, station["id"], "deactivate", {"reason": "maintenance"}
        )
        with self.assertRaises(ConflictError):
            self.service.create(
                Actor("obs-north", "operator", station["id"]),
                "measurement_batch",
                {
                    "candidate_id": self.candidate["id"],
                    "station_id": station["id"],
                    "measurements": [self._measurement("2026-09-30T22:00:00Z", 17.0)],
                },
            )

    def test_rejected_items_are_terminal_but_batch_is_retained(self):
        station = self._station("North", ["obs-north"])
        observer = Actor("obs-north", "operator", station["id"])
        batch = self._batch(
            observer,
            station,
            [self._measurement("2026-09-30T22:00:00Z", 17.0)],
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(observer, batch["id"], "retry", {})
        stored = self.service.get(batch["id"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["data"]["merged_count"], 1)

    def test_priority_is_invalidated_and_recalculated_on_new_brightness(self):
        before = self.service.get(self.candidate["id"])["data"]["priority_score"]
        station = self._station("North", ["obs-north"])
        observer = Actor("obs-north", "operator", station["id"])
        self._batch(
            observer,
            station,
            [self._measurement("2026-09-30T22:00:00Z", 15.0)],
        )
        candidate = self.service.get(self.candidate["id"])
        self.assertGreater(candidate["data"]["priority_score"], before)
        self.assertIn("priority_recalculated_at", candidate["data"])

        # An older measurement fills history but does not move latest brightness,
        # so the cached priority stays authoritative.
        priority_after_bright = candidate["data"]["priority_score"]
        stamp = candidate["data"]["priority_recalculated_at"]
        self._batch(
            observer,
            station,
            [self._measurement("2026-09-30T19:00:00Z", 13.0)],
        )
        candidate = self.service.get(self.candidate["id"])
        self.assertEqual(candidate["data"]["latest_magnitude"], 15.0)
        self.assertEqual(candidate["data"]["priority_score"], priority_after_bright)
        self.assertEqual(candidate["data"]["priority_recalculated_at"], stamp)

    def test_merging_measurements_never_alters_scheduled_observation(self):
        telescope = self.service.create(
            self.coordinator,
            "telescope",
            {"name": "North 2m", "aperture_m": 2.0, "site_name": "NAO"},
        )
        observation = self.service.create(
            self.coordinator,
            "observation",
            {
                "candidate_id": self.candidate["id"],
                "telescope_id": telescope["id"],
                "team_id": "team-north",
                "start_at": "2026-10-02T10:00:00Z",
                "end_at": "2026-10-02T11:00:00Z",
                "mode": "imaging",
            },
        )
        observation = self.service.transition(
            self.coordinator, observation["id"], "schedule", {"operator_id": "op-1"}
        )
        snapshot_data = dict(observation["data"])
        snapshot_version = observation["version"]

        station = self._station("North", ["obs-north"])
        self._batch(
            Actor("obs-north", "operator", station["id"]),
            station,
            [
                self._measurement("2026-09-30T22:00:00Z", 16.0),
                self._measurement("2026-09-30T23:00:00Z", 15.2),
            ],
        )
        observation = self.service.get(observation["id"])
        self.assertEqual(observation["status"], "scheduled")
        self.assertEqual(observation["version"], snapshot_version)
        self.assertEqual(observation["data"], snapshot_data)

    def test_single_merge_enforces_station_membership(self):
        station = self._station("North", ["obs-north"])
        other = self._station("South", ["obs-south"])
        payload = {
            "measurement": self._measurement("2026-09-30T22:00:00Z", 17.0),
            "station_id": station["id"],
        }
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("obs-south", "operator", station["id"]),
                self.candidate["id"],
                "merge_measurement",
                payload,
            )
        # An unassigned operator cannot borrow a station.
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("obs-stranger", "operator", station["id"]),
                self.candidate["id"],
                "merge_measurement",
                payload,
            )
        merged = self.service.transition(
            Actor("obs-north", "operator", station["id"]),
            self.candidate["id"],
            "merge_measurement",
            payload,
        )
        self.assertEqual(merged["data"]["measurements"][-1]["station_id"], station["id"])

    def test_retry_is_idempotent_and_audited(self):
        station = self._station("North", ["obs-north"])
        observer = Actor("obs-north", "operator", station["id"])
        batch = self._batch(
            observer,
            station,
            [self._measurement("2026-09-30T22:00:00Z", 17.0)],
        )
        candidate_count = self.service.get(self.candidate["id"])["data"][
            "merged_measurement_count"
        ]
        # Retrying a terminal batch is rejected rather than replaying.
        with self.assertRaises(InvalidTransition):
            self.service.transition(observer, batch["id"], "retry", {})
        self.assertEqual(
            self.service.get(self.candidate["id"])["data"]["merged_measurement_count"],
            candidate_count,
        )
        actions = [
            row["action"]
            for row in self.service.audit_log(batch["id"])
            if row["action"] in ("submit", "retry")
        ]
        self.assertEqual(actions, ["submit"])

    def test_simultaneous_station_submissions_converge_after_retry(self):
        north = self._station("North", ["obs-north"])
        south = self._station("South", ["obs-south"])
        queued = []
        barrier = threading.Barrier(2)

        def submit(station, observer_id, magnitudes):
            service = DomainService(self.repo, RuleEngine())
            observer = Actor(observer_id, "operator", station["id"])
            batch = service.create(
                observer,
                "measurement_batch",
                {
                    "candidate_id": self.candidate["id"],
                    "station_id": station["id"],
                    "measurements": [
                        self._measurement("2026-09-30T22:00:%02dZ" % index, magnitude)
                        for index, magnitude in enumerate(magnitudes)
                    ],
                },
            )
            queued.append((observer, batch["id"]))
            barrier.wait()
            service.transition(observer, batch["id"], "submit", {})

        errors = []

        def target(*args):
            try:
                submit(*args)
            except Exception as exc:  # pragma: no cover - surfaced via assertion
                errors.append(exc)

        threads = [
            threading.Thread(
                target=target,
                args=(north, "obs-north", (17.2, 17.0)),
            ),
            threading.Thread(
                target=target,
                args=(south, "obs-south", (17.4, 17.5)),
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])

        # First write never gets overwritten; every blocked item is retryable.
        batches = [
            self.service.transition(observer, batch_id, "retry", {})
            if self.service.get(batch_id)["status"] == "partial"
            else self.service.get(batch_id)
            for observer, batch_id in queued
        ]
        total_merged = sum(batch["data"]["merged_count"] for batch in batches)
        self.assertEqual(total_merged, 4)
        candidate = self.service.get(self.candidate["id"])
        self.assertEqual(candidate["data"]["merged_measurement_count"], 5)
        station_ids = {
            record.get("station_id")
            for record in candidate["data"]["measurements"]
            if record["observed_at"].startswith("2026-09-30T22:00")
        }
        self.assertEqual(station_ids, {north["id"], south["id"]})


class StationHttpTest(unittest.TestCase):
    def test_http_station_headers_enforce_authorization(self):
        import json
        import urllib.error
        import urllib.request

        from src.http_api import create_server

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = DomainService(
            SQLiteRepository(Path(tmp.name) / "http.db"), RuleEngine()
        )
        server = create_server("127.0.0.1", 0, service, RuleEngine(), tempfile.mkdtemp())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://%s:%s" % server.server_address

        def request(method, path, payload, headers):
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                base + path, data=data, method=method, headers=headers
            )
            try:
                with urllib.request.urlopen(req) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        analyst_headers = {"X-User-Id": "a1", "X-Role": "analyst"}
        status, source = request(
            "POST",
            "/api/sources",
            {"name": "Survey", "survey_name": "S"},
            analyst_headers,
        )
        self.assertEqual(status, 201)
        status, candidate = request(
            "POST",
            "/api/candidates",
            {
                "source_id": source["id"],
                "event_id": "AT-HTTP-1",
                "ra": 100,
                "dec": 10,
                "magnitude": 18,
                "transient_type": "unknown",
                "observed_at": "2026-09-30T20:00:00Z",
            },
            analyst_headers,
        )
        self.assertEqual(status, 201)
        coord_headers = {"X-User-Id": "c1", "X-Role": "coordinator"}
        status, north = request(
            "POST",
            "/api/stations",
            {"name": "North", "site_name": "Site", "observer_ids": ["obs-north"]},
            coord_headers,
        )
        self.assertEqual(status, 201)
        request("POST", "/api/entities/%s/actions" % north["id"], {"action": "activate"}, coord_headers)
        status, south = request(
            "POST",
            "/api/stations",
            {"name": "South", "site_name": "Site", "observer_ids": ["obs-south"]},
            coord_headers,
        )
        self.assertEqual(status, 201)
        request("POST", "/api/entities/%s/actions" % south["id"], {"action": "activate"}, coord_headers)

        measurement = {
            "observed_at": "2026-09-30T22:00:00Z",
            "ra": 100,
            "dec": 10,
            "magnitude": 17,
        }
        # An observer from the South station presenting the North station id
        # is rejected: observers may only submit their own station's data.
        status, body = request(
            "POST",
            "/api/entities/%s/actions" % candidate["id"],
            {
                "action": "merge_measurement",
                "data": {"measurement": measurement},
            },
            {"X-User-Id": "obs-south", "X-Role": "operator", "X-Station-Id": north["id"]},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["type"], "PermissionDenied")

        # The station's own observer merges through the HTTP API.
        status, updated = request(
            "POST",
            "/api/entities/%s/actions" % candidate["id"],
            {
                "action": "merge_measurement",
                "data": {"measurement": measurement},
            },
            {"X-User-Id": "obs-north", "X-Role": "operator", "X-Station-Id": north["id"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["data"]["measurements"][-1]["station_id"], north["id"])


if __name__ == "__main__":
    unittest.main()
