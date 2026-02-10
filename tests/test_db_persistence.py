"""Test SQLite persistence for operations."""

import asyncio
import os
import sys

# Use a test database
os.environ["NXFLO_DATABASE_URL"] = "sqlite+aiosqlite:///test_nxflo.db"

from src.buying.tracker import OperationTracker, TaskStatus


async def main():
    # Clean up any existing test db
    if os.path.exists("test_nxflo.db"):
        os.remove("test_nxflo.db")

    print("=== Test SQLite Persistence ===\n")

    # Create tracker and init DB
    tracker = OperationTracker()
    await tracker.init_db()
    print("1. DB initialized OK")

    # Create an operation
    op = tracker.create(
        operation_type="create_media_buy",
        seller_name="Test Seller",
        seller_url="https://test.example.com/mcp",
        buyer_ref="test-ref-001",
        request_data={"product_id": "prod-123", "budget": 1000},
    )
    await tracker._persist(op)
    print(f"2. Created operation {op.id[:8]}... OK")

    # Update it
    op = tracker.update_from_response(op.id, {
        "status": "submitted",
        "task_id": "task-abc-123",
        "context_id": "ctx-456",
    })
    await tracker._persist(op)
    print(f"3. Updated to status={op.status.value} OK")

    # Create a new tracker to test reload
    tracker2 = OperationTracker()
    await tracker2.init_db()
    print(f"4. Loaded {len(tracker2.list_all())} operations from DB")

    loaded = tracker2.get(op.id)
    assert loaded is not None, "Should load operation from DB"
    assert loaded.status == TaskStatus.SUBMITTED, f"Should be submitted, got {loaded.status}"
    assert loaded.task_id == "task-abc-123"
    assert loaded.buyer_ref == "test-ref-001"
    print(f"5. Verified: status={loaded.status.value}, task_id={loaded.task_id}, buyer_ref={loaded.buyer_ref}")

    # Mark completed and persist
    loaded = tracker2.update_from_response(loaded.id, {
        "status": "completed",
        "media_buy_id": "mb-789",
    })
    await tracker2._persist(loaded)
    print(f"6. Completed: media_buy_id={loaded.media_buy_id}")

    # Final reload
    tracker3 = OperationTracker()
    await tracker3.init_db()
    final = tracker3.get(op.id)
    assert final.status == TaskStatus.COMPLETED
    assert final.media_buy_id == "mb-789"
    print(f"7. Final reload: status={final.status.value}, media_buy_id={final.media_buy_id}")

    # Clean up (dispose engine to release SQLite file lock)
    from src.models.schema import engine
    await engine.dispose()
    os.remove("test_nxflo.db")
    print("\nAll persistence tests PASSED")


if __name__ == "__main__":
    asyncio.run(main())
