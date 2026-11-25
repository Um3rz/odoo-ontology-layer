# -*- coding: utf-8 -*-
"""Onto job tick (3.3). Written, not run: the canonical addon suite runs it at the phase end (plan 6.1).

Schedules are tenant-local, so one instant is a different day in different zones. A late tick starts the latest
occurrence once. A 409 or an outage leaves the job due, and neither raises. querygen is patched, so no test reaches
the network."""

from datetime import datetime
from unittest.mock import patch

import pytz
from odoo.tests.common import TransactionCase, tagged

from ..models import onto_jobs
from ..models.onto_jobs import JOBS, LAST_KEY, is_due, slot_start
from ..models.onto_query import QuerygenError

DAILY = next(job for job in JOBS if job.name == "daily_review")  # 06:00 tenant time
WEEKLY = next(job for job in JOBS if job.name == "weekly_brief")  # Monday 06:00 tenant time


@tagged("post_install", "-at_install")
class TestOntoJobs(TransactionCase):
    def setUp(self):
        super().setUp()
        self.env.company.partner_id.write({"tz": "Asia/Riyadh"})  # UTC+3 with no DST
        self.params = self.env["ir.config_parameter"].sudo()

    def _set_last(self, name, when):
        self.params.set_param(LAST_KEY % name, when.isoformat(timespec="seconds"))

    def _get_last(self, name):
        return self.params.get_param(LAST_KEY % name)

    def _tick(self, now, failure=None):
        """One tick at `now` (naive UTC). Returns the querygen paths it asked. `failure` is raised by every call."""
        asked = []

        def answer(env, path, payload, timeout):
            asked.append(path)
            if failure is not None:
                raise failure
            return {"accepted": True}

        with patch.object(onto_jobs, "onto_now", return_value=now), \
                patch.object(onto_jobs, "querygen_call", side_effect=answer):
            self.env["onto.jobs"]._onto_tick()
        return asked

    def test_due_is_tenant_local_across_timezones(self):
        riyadh = pytz.timezone("Asia/Riyadh")  # UTC+3: 06:00 local is 03:00 UTC
        new_york = pytz.timezone("America/New_York")  # UTC-5 in November: 06:00 local is 11:00 UTC
        auckland = pytz.timezone("Pacific/Auckland")  # UTC+13 in November: Monday 06:00 is Sunday 17:00 UTC
        self.assertEqual(slot_start(riyadh, datetime(2026, 11, 9, 2, 59), DAILY), datetime(2026, 11, 8, 3, 0))
        self.assertEqual(slot_start(riyadh, datetime(2026, 11, 9, 3, 0), DAILY), datetime(2026, 11, 9, 3, 0))
        self.assertEqual(slot_start(new_york, datetime(2026, 11, 9, 10, 59), DAILY), datetime(2026, 11, 8, 11, 0))
        # Monday 10:00 UTC is already Monday 23:00 in Auckland, so this week's brief is the latest occurrence.
        self.assertEqual(slot_start(auckland, datetime(2026, 11, 9, 10, 0), WEEKLY), datetime(2026, 11, 8, 17, 0))
        # One hour earlier it is still Monday 05:59 in Auckland, so last week's brief is the latest occurrence.
        self.assertEqual(slot_start(auckland, datetime(2026, 11, 8, 16, 59), WEEKLY), datetime(2026, 11, 1, 17, 0))

    def test_is_due_needs_a_success_before_the_occurrence(self):
        slot = datetime(2026, 11, 9, 3, 0)
        self.assertTrue(is_due(datetime(2026, 11, 9, 2, 59), slot))
        self.assertFalse(is_due(slot, slot))
        self.assertFalse(is_due(None, slot))  # never ran: the caller baselines it instead
        self.assertFalse(is_due(datetime(2026, 11, 8, 0, 0), None))

    def test_never_run_job_starts_from_its_next_occurrence(self):
        now = datetime(2026, 11, 9, 10, 0)  # after this morning's 06:00 slot, so nothing is owed
        self.assertEqual(self._tick(now), [])
        self.assertEqual(self._get_last("daily_review"), "2026-11-09T10:00:00")

    def test_late_tick_starts_the_latest_occurrence_once(self):
        self._set_last("daily_review", datetime(2026, 11, 5, 3, 0))  # four days late
        now = datetime(2026, 11, 9, 10, 0)  # 13:00 in Riyadh, after the 06:00 slot
        self.assertEqual(self._tick(now), ["systems/run/daily_review"])
        self.assertEqual(self._tick(now), [])  # the same occurrence already has its success

    def test_409_keeps_the_job_due_and_does_not_raise(self):
        self._set_last("daily_review", datetime(2026, 11, 8, 0, 0))
        now = datetime(2026, 11, 9, 10, 0)
        self.assertEqual(self._tick(now, QuerygenError(409)), ["systems/run/daily_review"])
        self.assertEqual(self._get_last("daily_review"), "2026-11-08T00:00:00")  # still owed
        self.assertEqual(self._tick(now), ["systems/run/daily_review"])  # the next tick retries and succeeds
        self.assertEqual(self._get_last("daily_review"), "2026-11-09T10:00:00")

    def test_outage_logs_and_does_not_raise(self):
        self._set_last("daily_review", datetime(2026, 11, 8, 0, 0))
        now = datetime(2026, 11, 9, 10, 0)
        for failure in (QuerygenError(None), ConnectionError("querygen unreachable")):
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(self._tick(now, failure), ["systems/run/daily_review"])
                self.assertEqual(self._get_last("daily_review"), "2026-11-08T00:00:00")
