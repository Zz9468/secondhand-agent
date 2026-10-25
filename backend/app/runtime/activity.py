"""Durable execution time: charge lost attempts through their last granted lease."""

from datetime import timedelta

from app.db.models.graph import NotificationDelivery
from app.runtime.contracts import BudgetExhausted


def used_ms(run, now):
    elapsed = 0
    if run.active_since and run.active_until:
        elapsed = max(
            0, int((min(now, run.active_until) - run.active_since).total_seconds() * 1000)
        )
    return (run.active_ms or 0) + elapsed


def settle(run, now):
    if run.active_since and not run.closing_deadline and remaining(run, now) <= 0:
        run.closing_deadline = run.active_since + timedelta(
            seconds=max(0, run.config.get("active_seconds", 120) - run.active_ms / 1000) + 15
        )
    run.active_ms = used_ms(run, now)
    run.active_since = run.active_until = None


def remaining(run, now):
    return max(0, run.config.get("active_seconds", 120) - used_ms(run, now) / 1000)


def model_timeout(db, run, now):
    seconds = min(45, remaining(run, now))
    delivery = db.get(NotificationDelivery, f"turn:{run.id}:reply")
    if delivery:
        seconds = min(seconds, (delivery.reply_deadline - now).total_seconds())
    if run.closing_deadline or seconds <= 0:
        raise BudgetExhausted("活动预算或回复期限耗尽")
    return seconds


def lease_deadline(db, run, now, lease_seconds=30):
    if not run.closing_deadline:
        deadline = now + timedelta(seconds=remaining(run, now))
        delivery = db.get(NotificationDelivery, f"turn:{run.id}:reply")
        if delivery:
            deadline = min(deadline, delivery.reply_deadline)
        # Freeze the final no-model window, including across restart/manual recovery.
        if deadline <= now:
            run.closing_deadline = deadline + timedelta(seconds=15)
        else:
            return min(now + timedelta(seconds=lease_seconds), deadline + timedelta(seconds=15))
    return min(now + timedelta(seconds=lease_seconds), run.closing_deadline)
