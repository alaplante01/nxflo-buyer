"""Background polling service for pending AdCP operations.

Polls seller agents for status updates on async operations
(submitted, working) at protocol-recommended intervals.

Reference: https://docs.adcontextprotocol.org/docs/building/integration/mcp-guide
"""

import asyncio
import logging
from collections.abc import Callable

from src.buying.tracker import (
    OperationTracker,
    TrackedOperation,
    TERMINAL_STATUSES,
)
from src.connections.seller import tasks_get
from src.discovery.registry import SellerAgent

logger = logging.getLogger(__name__)


class BackgroundPoller:
    """Polls pending operations on a background schedule.

    Groups operations by status and polls at the appropriate interval:
    - WORKING: every 5 seconds (will finish soon)
    - SUBMITTED: every 60 seconds (long-running, hours/days)
    - INPUT_REQUIRED: skipped (waiting for human input)

    Accepts a callable for the seller list so it always reflects the
    latest discovery state (avoids stale references).
    """

    def __init__(
        self,
        tracker: OperationTracker,
        get_sellers: Callable[[], list[SellerAgent]],
    ):
        self.tracker = tracker
        self._get_sellers = get_sellers
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Background poller started")

    async def stop(self):
        """Stop the background polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Background poller stopped")

    async def _poll_loop(self):
        """Main polling loop.

        Checks for pending operations every 5 seconds (the minimum interval).
        Only polls each operation when its interval has elapsed.
        """
        poll_timestamps: dict[str, float] = {}  # op_id -> last poll time

        while self._running:
            try:
                pending = self.tracker.get_pending()

                for op in pending:
                    if not op.task_id:
                        continue

                    # Check if enough time has elapsed since last poll
                    interval = self.tracker.get_poll_interval(op)
                    if interval is None:
                        continue  # Don't poll (e.g., input-required)

                    now = asyncio.get_event_loop().time()
                    last = poll_timestamps.get(op.id, 0)
                    if now - last < interval:
                        continue

                    seller = self._find_seller(op)
                    if not seller:
                        continue

                    await self._poll_single(op, seller)
                    poll_timestamps[op.id] = now

                    # Clean up completed operations from timestamps
                    if op.status in TERMINAL_STATUSES:
                        poll_timestamps.pop(op.id, None)

            except Exception as e:
                logger.error(f"Poller error: {e}")

            await asyncio.sleep(5)

    async def _poll_single(self, op: TrackedOperation, seller: SellerAgent):
        """Poll a single operation and update tracker."""
        try:
            response = await tasks_get(seller, task_id=op.task_id, include_result=True)
            self.tracker.update_from_response(op.id, response)
            op.poll_count += 1
            await self.tracker._persist(op)

            if op.status in TERMINAL_STATUSES:
                logger.info(
                    f"Operation {op.id} completed: {op.status.value} "
                    f"(polled {op.poll_count} times)"
                )
        except Exception as e:
            logger.warning(f"Failed to poll operation {op.id}: {e}")

    def _find_seller(self, op: TrackedOperation) -> SellerAgent | None:
        """Find the seller agent for an operation by URL (always uses fresh list)."""
        op_url = op.seller_url.rstrip("/")
        for seller in self._get_sellers():
            if seller.url.rstrip("/") == op_url:
                return seller
        return None
