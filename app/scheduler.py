"""
Scheduler — recurring conversation stimuli for longitudinal studies.

Runs a background thread that checks panel_schedules every 60 seconds.
When a schedule's next_run_at has passed, submits the stimulus to the
response engine and updates the schedule.
"""

import logging
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

import psycopg2.extras

log = logging.getLogger(__name__)


def _parse_cron(expr):
    """Parse a simple cron expression and return next run time from now.

    Supports: minute hour day_of_month month day_of_week
    Special shortcuts: @hourly, @daily, @weekly, @monthly
    For simplicity, supports interval-based scheduling too: e.g. 'every 6h', 'every 30m'
    """
    now = datetime.now(timezone.utc)

    if expr.startswith("every "):
        val = expr[6:].strip()
        if val.endswith("h"):
            hours = int(val[:-1])
            return now + timedelta(hours=hours)
        elif val.endswith("m"):
            minutes = int(val[:-1])
            return now + timedelta(minutes=minutes)
        elif val.endswith("d"):
            days = int(val[:-1])
            return now + timedelta(days=days)
        return now + timedelta(hours=24)

    shortcuts = {
        "@hourly": timedelta(hours=1),
        "@daily": timedelta(days=1),
        "@weekly": timedelta(weeks=1),
        "@monthly": timedelta(days=30),
    }
    if expr in shortcuts:
        return now + shortcuts[expr]

    # Default: daily.
    return now + timedelta(days=1)


class ConversationScheduler:
    def __init__(self, db_pool, stimulus_fn):
        """
        Args:
            db_pool: psycopg2 ThreadedConnectionPool
            stimulus_fn: callable(method, path, body, timeout) -> (status, data)
        """
        self.db_pool = db_pool
        self.stimulus_fn = stimulus_fn
        self._running = False
        self._thread = None

    def start(self):
        """Start the scheduler background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="scheduler")
        self._thread.start()
        log.info("Conversation scheduler started")

    def stop(self):
        self._running = False

    def _run_loop(self):
        while self._running:
            try:
                self._check_schedules()
            except Exception as e:
                log.error("Scheduler error: %s", e)
            time.sleep(60)

    def _check_schedules(self):
        conn = self.db_pool.getconn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT id, panel_id, conversation_id, stimulus, stimulus_type,
                       sample_size, cron_expression, run_count, max_runs
                FROM panel_schedules
                WHERE status = 'active'
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= NOW()
            """)
            due = cur.fetchall()
            cur.close()
        finally:
            self.db_pool.putconn(conn)

        for schedule in due:
            self._execute_schedule(schedule)

    def _execute_schedule(self, schedule):
        sid = schedule["id"]
        log.info("Executing schedule %s for panel %s", sid, schedule["panel_id"])

        # Submit stimulus via the conversation ask flow.
        payload = {
            "stimulus": schedule["stimulus"],
            "stimulus_type": schedule["stimulus_type"],
        }
        if schedule["sample_size"] and schedule["sample_size"] > 0:
            payload["sample_size"] = schedule["sample_size"]

        status_code, result = self.stimulus_fn(
            "POST",
            f"/api/soul/panels/{schedule['panel_id']}/stimulate",
            payload,
            timeout=60,
        )

        run_id = result.get("run_id") if isinstance(result, dict) else None

        # Create a conversation turn for this scheduled run.
        conn = self.db_pool.getconn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

            # Get next turn number.
            cur.execute("""
                SELECT COALESCE(MAX(turn_number), 0) + 1 AS next_turn
                FROM panel_conversation_turns WHERE conversation_id = %s
            """, (schedule["conversation_id"],))
            next_turn = cur.fetchone()["next_turn"]

            turn_id = uuid.uuid4()
            cur.execute("""
                INSERT INTO panel_conversation_turns
                    (id, conversation_id, turn_number, stimulus, stimulus_type, run_id)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (turn_id, schedule["conversation_id"], next_turn,
                  schedule["stimulus"], schedule["stimulus_type"], run_id))

            # Update schedule.
            new_run_count = schedule["run_count"] + 1
            max_runs = schedule["max_runs"]
            if max_runs > 0 and new_run_count >= max_runs:
                new_status = "complete"
                next_run = None
            else:
                new_status = "active"
                next_run = _parse_cron(schedule["cron_expression"])

            cur.execute("""
                UPDATE panel_schedules
                SET last_run_at = NOW(), run_count = %s, next_run_at = %s, status = %s
                WHERE id = %s
            """, (new_run_count, next_run, new_status, sid))

            conn.commit()
            cur.close()
            log.info("Schedule %s: turn %d submitted (run %d/%s)",
                     sid, next_turn, new_run_count, max_runs or "unlimited")
        except Exception as e:
            conn.rollback()
            log.error("Schedule %s execution failed: %s", sid, e)
        finally:
            self.db_pool.putconn(conn)
