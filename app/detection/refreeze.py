"""Operator-confirmed refreeze of the frozen reference.

A refreeze is an explicit operator action: the target page is retrieved once
with the same disciplined fetcher the check loop uses, and only a successful,
validated retrieval replaces the current reference.  The new immutable
reference and its success event are written in one transaction by
:meth:`Store.refreeze_reference`.

A retrieval failure records a failed event and leaves the current reference
untouched, so checks keep comparing against the last usable reference.  A
refreeze never creates a check, never emits a change event and never
acknowledges an episode: it only resets the comparison baseline.

Page content is untrusted data.  It is stored as a canonical snapshot, never
logged, and never rendered by the refreeze page (only its hash is shown).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..logging_setup import StructuredLogger
from .fetch import FailureCategory, FetchFailure, FetchResult, Fetcher
from .locking import reference_lock
from .store import (
    ReferenceRecord,
    ReferenceSeed,
    RefreezeEventRecord,
    Store,
    utc_now_iso,
)

DEFAULT_FAILURE_MESSAGE = "la page n'a pas pu être récupérée"
SUCCESS_MESSAGE = "référence remplacée par l'opérateur"


@dataclass(frozen=True)
class RefreezeResult:
    """Outcome of one operator-confirmed refreeze attempt."""

    success: bool
    event: RefreezeEventRecord
    previous_reference: ReferenceRecord | None = None
    reference: ReferenceRecord | None = None
    failure_category: str | None = None
    failure_message: str | None = None


class RefreezeService:
    """Retrieve the target and atomically replace the frozen reference."""

    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        *,
        fetcher: Fetcher | None = None,
        logger: StructuredLogger | None = None,
    ) -> None:
        self._config = config
        self._store = store if store is not None else Store(config.database_path)
        self._fetcher = (
            fetcher if fetcher is not None else Fetcher(config, logger=logger)
        )
        self._logger = logger

    @property
    def store(self) -> Store:
        return self._store

    def refreeze(self, *, operator_label: str | None = None) -> RefreezeResult:
        """Perform one refreeze attempt.

        ``operator_label`` defaults to the configured operator.  The label is
        recorded on both the reference and the history event.
        """

        operator = (
            operator_label
            if operator_label is not None
            else self._config.operator_label
        )
        # Serialize with the check loop.  A check that is already running
        # finishes first, so its result can never be recorded against the
        # reference this refreeze is about to replace.
        with reference_lock(self._store.database_path):
            previous = self._store.current_reference()
            result = self._retrieve()

            if result.failure is not None or result.snapshot is None:
                return self._record_failure(result, operator, previous)

            seed = ReferenceSeed(
                target_url=self._config.target_url,
                snapshot=result.snapshot,
                operator_label=operator,
                captured_at=utc_now_iso(),
                http_status=result.http_status,
                etag=result.etag,
                last_modified=result.last_modified,
                content_type=result.content_type,
                previous_reference_id=(
                    previous.id if previous is not None else None
                ),
            )
            reference, event = self._store.refreeze_reference(
                seed,
                operator_label=operator,
                message=SUCCESS_MESSAGE,
            )
            self._log(
                "refreeze_completed",
                success=True,
                event_id=event.id,
                reference_id=reference.id,
                previous_reference_id=(
                    previous.id if previous is not None else None
                ),
            )
            return RefreezeResult(
                success=True,
                event=event,
                previous_reference=previous,
                reference=reference,
            )

    # -- internals ------------------------------------------------------

    def _retrieve(self) -> FetchResult:
        try:
            return self._fetcher.fetch()
        except Exception as exc:  # a fetcher bug must not lose the old reference
            self._log(
                "refreeze_fetch_error",
                success=False,
                error=type(exc).__name__,
            )
            return FetchResult(
                failure=FetchFailure(
                    FailureCategory.NETWORK_ERROR, DEFAULT_FAILURE_MESSAGE
                )
            )

    def _record_failure(
        self,
        result: FetchResult,
        operator: str,
        previous: ReferenceRecord | None,
    ) -> RefreezeResult:
        failure = result.failure or FetchFailure(
            FailureCategory.NETWORK_ERROR, DEFAULT_FAILURE_MESSAGE
        )
        event = self._store.record_refreeze_event(
            success=False,
            operator_label=operator,
            previous_reference_id=previous.id if previous is not None else None,
            message=failure.message,
        )
        self._log(
            "refreeze_completed",
            success=False,
            event_id=event.id,
            failure_category=failure.category.value,
        )
        return RefreezeResult(
            success=False,
            event=event,
            previous_reference=previous,
            failure_category=failure.category.value,
            failure_message=failure.message,
        )

    def _log(self, event: str, **fields: object) -> None:
        if self._logger is not None:
            self._logger.log(event, **fields)


__all__ = [
    "DEFAULT_FAILURE_MESSAGE",
    "RefreezeResult",
    "RefreezeService",
    "SUCCESS_MESSAGE",
]
