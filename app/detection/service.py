"""One check cycle: retrieve, compare, persist, then notify.

Each cycle performs exactly one retrieval, compares the canonical snapshot with
the frozen reference, stores the outcome and every rule result in one
transaction, and only afterwards calls the optional change sink.  A control
failure is never reported as a content change and never reaches the sink.

Overlapping cycles are refused by a non-blocking lock: the scheduler and a
manual run can never retrieve the page twice at the same time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ..config import Config
from ..logging_setup import StructuredLogger
from .canonical import CanonicalSnapshot, SnapshotFormatError
from .engine import (
    STATUS_NOT_EVALUATED,
    Evaluation,
    EvaluationError,
    RuleResult,
    evaluate,
)
from .fetch import FailureCategory, FetchFailure, FetchResult, Fetcher
from .locking import reference_lock
from .rules import Rule
from .store import (
    CheckRecord,
    ReferenceConflictError,
    ReferenceSeed,
    Store,
    utc_now_iso,
)

OUTCOME_UNCHANGED = "unchanged"
OUTCOME_CHANGED = "changed"
OUTCOME_CONTROL_FAILURE = "control_failure"

#: Internal marker for a cycle that never started because another one is
#: running.  It is deliberately never written to the ``checks`` table.
OUTCOME_SKIPPED = "skipped"

REASON_REFERENCE_INITIALIZED = "reference_initialized"

#: Internal reason for a cycle abandoned because a refreeze won the race.
REASON_REFERENCE_CHANGED = "reference_changed"

DEFAULT_FAILURE_MESSAGE = "la page n'a pas pu être récupérée"


@dataclass(frozen=True)
class ChangeEvent:
    """Delivered to the change sink after a changed check is committed."""

    check_id: int
    target_url: str
    triggered_rule_ids: tuple[str, ...]
    detected_at: str


@dataclass(frozen=True)
class CheckRun:
    """Outcome of one :meth:`CheckService.run_once` call."""

    outcome: str
    check_id: int | None = None
    reason: str | None = None
    skipped: bool = False
    failure_category: str | None = None
    triggered_rule_ids: tuple[str, ...] = ()
    results: tuple[RuleResult, ...] = ()

    @property
    def changed(self) -> bool:
        return self.outcome == OUTCOME_CHANGED

    @property
    def failed(self) -> bool:
        return self.outcome == OUTCOME_CONTROL_FAILURE


ChangeSink = Callable[[ChangeEvent], None]


class CheckService:
    """Run check cycles against the configured target."""

    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        *,
        fetcher: Fetcher | None = None,
        change_sink: ChangeSink | None = None,
        logger: StructuredLogger | None = None,
        seed_default_rules: bool = True,
    ) -> None:
        self._config = config
        self._store = store if store is not None else Store(config.database_path)
        self._fetcher = (
            fetcher if fetcher is not None else Fetcher(config, logger=logger)
        )
        self._change_sink = change_sink
        self._logger = logger
        # Shared with RefreezeService so a check and a refreeze can never
        # interleave their reference reads and writes in this process.
        self._run_lock = reference_lock(self._store.database_path)
        if seed_default_rules:
            inserted = self._store.seed_default_rules()
            if inserted:
                self._log("default_rules_seeded", inserted=inserted)

    @property
    def store(self) -> Store:
        return self._store

    def run_once(self) -> CheckRun:
        """Run one full cycle; skip immediately if a cycle is already running."""

        if not self._run_lock.acquire(blocking=False):
            self._log("check_skipped", reason="overlap")
            return CheckRun(outcome=OUTCOME_SKIPPED, skipped=True)

        started_at = utc_now_iso()
        started_monotonic = time.monotonic()
        try:
            self._log("check_started", target_url=self._config.target_url)
            result = self._retrieve()
            if result.failure is not None or result.snapshot is None:
                return self._record_control_failure(
                    result, started_at, started_monotonic
                )
            return self._record_success(result, started_at, started_monotonic)
        finally:
            self._run_lock.release()

    # -- retrieval ------------------------------------------------------

    def _retrieve(self) -> FetchResult:
        try:
            return self._fetcher.fetch()
        except Exception as exc:  # a fetcher bug must not crash the loop
            self._log("check_fetch_error", error=type(exc).__name__)
            return FetchResult(
                failure=FetchFailure(
                    FailureCategory.NETWORK_ERROR, DEFAULT_FAILURE_MESSAGE
                )
            )

    # -- recording ------------------------------------------------------

    def _record_control_failure(
        self,
        result: FetchResult,
        started_at: str,
        started_monotonic: float,
    ) -> CheckRun:
        failure = result.failure or FetchFailure(
            FailureCategory.NETWORK_ERROR, DEFAULT_FAILURE_MESSAGE
        )
        finished_at = utc_now_iso()
        duration_ms = _duration_ms(started_monotonic)
        current = self._store.current_reference()
        rules = self._store.list_rules()
        results = tuple(
            RuleResult(
                rule_id=rule.id,
                rule_name=rule.name,
                kind=rule.kind,
                status=STATUS_NOT_EVALUATED,
                detail=(
                    "Contrôle en échec "
                    f"({failure.category.value}) : aucune comparaison effectuée."
                ),
            )
            for rule in rules
        )
        check = self._store.record_check(
            started_at=started_at,
            finished_at=finished_at,
            outcome=OUTCOME_CONTROL_FAILURE,
            reason=None,
            target_url=self._config.target_url,
            http_status=result.http_status,
            etag=result.etag,
            last_modified=result.last_modified,
            reference_id=current.id if current is not None else None,
            failure_category=failure.category.value,
            failure_message=failure.message,
            duration_ms=duration_ms,
            results=results,
        )
        self._log(
            "check_completed",
            outcome=OUTCOME_CONTROL_FAILURE,
            check_id=check.id,
            failure_category=failure.category.value,
            rule_count=len(results),
            duration_ms=duration_ms,
        )
        return CheckRun(
            outcome=OUTCOME_CONTROL_FAILURE,
            check_id=check.id,
            failure_category=failure.category.value,
            results=results,
        )

    def _record_success(
        self,
        result: FetchResult,
        started_at: str,
        started_monotonic: float,
    ) -> CheckRun:
        snapshot = result.snapshot
        assert snapshot is not None  # guaranteed by run_once
        if not snapshot.text.strip() or not snapshot.blocks:
            # Defensive boundary check: a fetcher must never turn an unusable
            # page into a content change, even if it returns a snapshot.
            return self._record_control_failure(
                FetchResult(
                    failure=FetchFailure(
                        FailureCategory.UNPARSEABLE_CONTENT,
                        "le contenu récupéré ne contient aucune structure "
                        "exploitable",
                    ),
                    http_status=result.http_status,
                    etag=result.etag,
                    last_modified=result.last_modified,
                ),
                started_at,
                started_monotonic,
            )
        finished_at = utc_now_iso()
        duration_ms = _duration_ms(started_monotonic)
        rules = self._store.list_rules()

        current = self._store.current_reference()
        if current is None:
            return self._capture_initial_reference(
                result,
                snapshot,
                rules,
                started_at,
                finished_at,
                duration_ms,
            )
        if self._store.current_reference_id() != current.id:
            return self._skip_reference_changed()

        try:
            baseline = current.to_snapshot()
        except SnapshotFormatError:
            return self._record_control_failure(
                FetchResult(
                    failure=FetchFailure(
                        FailureCategory.UNPARSEABLE_CONTENT,
                        "la référence de contrôle enregistrée est illisible",
                    ),
                    http_status=result.http_status,
                    etag=result.etag,
                    last_modified=result.last_modified,
                ),
                started_at,
                started_monotonic,
            )

        try:
            evaluation = evaluate(baseline, snapshot, rules)
        except EvaluationError:
            return self._record_control_failure(
                FetchResult(
                    failure=FetchFailure(
                        FailureCategory.UNPARSEABLE_CONTENT,
                        "la comparaison avec la référence a échoué",
                    ),
                    http_status=result.http_status,
                    etag=result.etag,
                    last_modified=result.last_modified,
                ),
                started_at,
                started_monotonic,
            )

        outcome = OUTCOME_CHANGED if evaluation.changed else OUTCOME_UNCHANGED
        if self._store.current_reference_id() != current.id:
            return self._skip_reference_changed()
        try:
            check = self._store.record_check(
                started_at=started_at,
                finished_at=finished_at,
                outcome=outcome,
                reason=None,
                target_url=self._config.target_url,
                http_status=result.http_status,
                etag=result.etag,
                last_modified=result.last_modified,
                reference_id=current.id,
                duration_ms=duration_ms,
                results=evaluation.results,
                expected_reference_id=current.id,
            )
        except ReferenceConflictError:
            return self._skip_reference_changed()
        self._log(
            "check_completed",
            outcome=outcome,
            check_id=check.id,
            rule_count=len(evaluation.results),
            triggered_count=len(evaluation.triggered_rule_ids),
            triggered_rules=list(evaluation.triggered_rule_ids),
            duration_ms=duration_ms,
        )
        run = CheckRun(
            outcome=outcome,
            check_id=check.id,
            triggered_rule_ids=evaluation.triggered_rule_ids,
            results=evaluation.results,
        )
        if evaluation.changed:
            # The check and its results are committed; notify the episode layer
            # afterwards so it can never observe a rollback or a failed check.
            self._emit_change(check, evaluation, finished_at)
        return run

    def _capture_initial_reference(
        self,
        result: FetchResult,
        snapshot: CanonicalSnapshot,
        rules: list[Rule],
        started_at: str,
        finished_at: str,
        duration_ms: int,
    ) -> CheckRun:
        evaluation = evaluate(snapshot, snapshot, rules)
        seed = ReferenceSeed(
            target_url=self._config.target_url,
            snapshot=snapshot,
            operator_label=self._config.operator_label,
            captured_at=finished_at,
            http_status=result.http_status,
            etag=result.etag,
            last_modified=result.last_modified,
            content_type=result.content_type,
        )
        try:
            check = self._store.record_check(
                started_at=started_at,
                finished_at=finished_at,
                outcome=OUTCOME_UNCHANGED,
                reason=REASON_REFERENCE_INITIALIZED,
                target_url=self._config.target_url,
                http_status=result.http_status,
                etag=result.etag,
                last_modified=result.last_modified,
                duration_ms=duration_ms,
                results=evaluation.results,
                new_reference=seed,
                expected_reference_id=None,
            )
        except ReferenceConflictError:
            # Another writer created a reference after we read ``None``.
            # Never silently supersede an operator-confirmed reference.
            return self._skip_reference_changed()
        self._log(
            "check_completed",
            outcome=OUTCOME_UNCHANGED,
            reason=REASON_REFERENCE_INITIALIZED,
            check_id=check.id,
            reference_id=check.reference_id,
            rule_count=len(evaluation.results),
            duration_ms=duration_ms,
        )
        return CheckRun(
            outcome=OUTCOME_UNCHANGED,
            check_id=check.id,
            reason=REASON_REFERENCE_INITIALIZED,
            results=evaluation.results,
        )

    def _skip_reference_changed(self) -> CheckRun:
        """Abandon a cycle whose reference was superseded before commit."""

        self._log("check_skipped", reason=REASON_REFERENCE_CHANGED)
        return CheckRun(
            outcome=OUTCOME_SKIPPED,
            reason=REASON_REFERENCE_CHANGED,
            skipped=True,
        )

    def _emit_change(
        self, check: CheckRecord, evaluation: Evaluation, detected_at: str
    ) -> None:
        if self._change_sink is None:
            return
        event = ChangeEvent(
            check_id=check.id,
            target_url=check.target_url,
            triggered_rule_ids=evaluation.triggered_rule_ids,
            detected_at=detected_at,
        )
        try:
            self._change_sink(event)
        except Exception as exc:
            # The check is already committed.  A failing consumer must not turn
            # a recorded change into a failed cycle or lose the check row.
            self._log(
                "change_sink_failed",
                check_id=check.id,
                error=type(exc).__name__,
            )

    # -- logging --------------------------------------------------------

    def _log(self, event: str, **fields: object) -> None:
        if self._logger is None:
            return
        self._logger.log(event, **fields)


def _duration_ms(started_monotonic: float) -> int:
    return max(0, int(round((time.monotonic() - started_monotonic) * 1000)))


__all__ = [
    "ChangeEvent",
    "CheckRun",
    "CheckService",
    "OUTCOME_CHANGED",
    "OUTCOME_CONTROL_FAILURE",
    "OUTCOME_SKIPPED",
    "OUTCOME_UNCHANGED",
    "REASON_REFERENCE_CHANGED",
    "REASON_REFERENCE_INITIALIZED",
]
