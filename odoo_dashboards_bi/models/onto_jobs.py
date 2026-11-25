# -*- coding: utf-8 -*-
"""The one Onto cron (3.3, plan 6.1): onto_tick runs each job that is due, then the stock history, in one budget.

A job is due when the latest occurrence of its schedule has no success yet. The schedule is tenant-local (the
company partner's timezone, as in onto_query), so a Monday 06:00 brief is Sunday 17:00 UTC in Auckland. A late or
skipped tick catches up: after a long gap the first tick starts the latest occurrence once, not every missed one.
A job that has never run is baselined at now, so installing Onto starts nothing at once.

A querygen job is one POST (3.5). 202 records the start. 409 (the previous run is still going) and any failure leave
the job due for the next tick and cost one log line. Nothing here raises for a querygen outage.

The stock history (3.10) is the last step. It keeps its own cursor, so it is always due and does nothing when caught
up. The status refresh (4.2) and the SLA check (7.4) are later steps of this tick.

Pausing onto_tick pauses these jobs and the stock history for the tenant. The weekly parity cron (1.8) stays a separate
cron: its check is not budgeted, so it cannot run inside a 30 s tick.

ponytail: a job that fails through a whole occurrence is abandoned at the next occurrence, not retried for the old one.
"""

import logging
import time
from collections import namedtuple
from datetime import datetime, time as dtime, timedelta

import pytz

from odoo import api, models

from .onto_query import QuerygenError, querygen_call
from .onto_stock_daily import _zone
from .onto_store import onto_now
from .onto_token import mint_bot_token

_logger = logging.getLogger(__name__)

TICK_BUDGET = 30  # seconds per tick (plan 6.1)
STEP_MARGIN = 10  # a step starts only with this much budget left: one statement can run that long
JOB_TIMEOUT = 3  # seconds for the POST that starts a querygen job (3.5)
TICK_XMLID = "odoo_dashboards_bi.ir_cron_onto_tick"
LAST_KEY = "onto.job.%s.last_success"  # ir.config_parameter: when the job last started (naive UTC, ISO)

# A tenant-local schedule: daily at `hour`, or weekly on `weekday` (0 = Monday) at `hour`. Tuple order is run order.
Job = namedtuple("Job", "name hour weekday")
JOBS = (
    Job("daily_review", 6, None),
    Job("weekly_brief", 6, 0),
    Job("forecast", 2, None),
    Job("recommend", 3, None),  # after forecast
    Job("backtest", 4, 6),
    Job("drift", 5, 6),
)


def slot_start(zone, now, job):
    """Start of the latest occurrence of the job's schedule that is not after `now` (naive UTC, as onto_now() is)."""
    now_aware = pytz.utc.localize(now)
    today = now_aware.astimezone(zone).date()
    for back in range(8):
        day = today - timedelta(days=back)
        if job.weekday is not None and day.weekday() != job.weekday:
            continue
        start = zone.localize(datetime.combine(day, dtime(job.hour)))
        if start <= now_aware:
            return start.astimezone(pytz.utc).replace(tzinfo=None)
    return None


def is_due(last, slot):
    """True when the occurrence at `slot` has no success yet. `last` is the last success (naive UTC), None if never."""
    return last is not None and slot is not None and last < slot


def _spent(started):
    return time.monotonic() - started


class OntoJobs(models.AbstractModel):
    _name = "onto.jobs"
    _description = "Onto job tick: runs the due jobs and the stock history (cron onto_tick)"

    @api.model
    def _onto_tick(self):
        """The cron (data/onto_cron.xml, every 5 minutes). Runs the due jobs, then the stock history, inside
        TICK_BUDGET. Asks for another tick when work is left over."""
        self = self.sudo()
        started = time.monotonic()
        zone, now = _zone(self.env), onto_now()
        due = [job for job in JOBS if self._onto_job_due(job, zone, now)]
        more = False
        for job in due:
            if _spent(started) > TICK_BUDGET - STEP_MARGIN:
                more = True
                break
            self._onto_start_job(job)
        if _spent(started) <= TICK_BUDGET - STEP_MARGIN:
            left = self.env["onto.stock.daily"]._onto_run_days(TICK_BUDGET - STEP_MARGIN - _spent(started))["left"]
            more = more or left > 0
        else:
            more = True
        if more:
            self.env.ref(TICK_XMLID)._trigger()
        _logger.info("Onto tick: %d job(s) due, %.1f s", len(due), _spent(started))

    def _onto_job_due(self, job, zone, now):
        """True when the job's latest occurrence has no success yet. A job that never ran is baselined at now: its first
        run is the next occurrence."""
        params = self.env["ir.config_parameter"]
        raw = params.get_param(LAST_KEY % job.name)
        if not raw:
            params.set_param(LAST_KEY % job.name, now.isoformat(timespec="seconds"))
            return False
        return is_due(datetime.fromisoformat(raw), slot_start(zone, now, job))

    def _onto_start_job(self, job):
        """One querygen job (3.5). 202 records the start. 409 and every failure leave it due for the next tick.
        Never raises: an outage costs one log line."""
        try:
            with self.env.cr.savepoint():
                params = self.env["ir.config_parameter"]
                body = {
                    "odoo_url": params.get_param("web.base.url"),
                    "db": params.get_param("database.uuid"),
                    "bot_token": mint_bot_token(self.env),
                    "params": {},
                }
                querygen_call(self.env, "systems/run/%s" % job.name, body, JOB_TIMEOUT)
        except QuerygenError as exc:
            if exc.status == 409:
                _logger.info("Onto job %s: the previous run is still going; retried next tick", job.name)
            else:
                _logger.warning("Onto job %s not started (querygen status %s); retried next tick",
                                job.name, exc.status)
            return False
        except Exception as exc:  # noqa: BLE001 - one job must never stop the tick; only the type is logged
            _logger.warning("Onto job %s not started (%s); retried next tick", job.name, type(exc).__name__)
            return False
        self.env["ir.config_parameter"].set_param(LAST_KEY % job.name, onto_now().isoformat(timespec="seconds"))
        self.env.cr.commit()  # the start stays recorded when a later step fails
        _logger.info("Onto job %s started", job.name)
        return True
