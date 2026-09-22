"""Storage Layer for NSE Data Scrapers.

Currently implements a lightweight JSON-backed storage layer for parsed results.
Designed with clear interface boundaries so it can be swapped for SQLite,
PostgreSQL, or an ORM like SQLAlchemy if historical storage, time-series analysis,
or multiple complex data sources are introduced later.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import BASE_DIR, Config

logger = logging.getLogger(__name__)


class StorageInterface:
    """Abstract/Reference Interface for storage implementations.
    
    To swap for SQLite or another database, implement this interface:
    - save_volume_gainers(symbols, count, fetched_at)
    - get_volume_gainers()
    - get_last_fetched_timestamp()
    - is_cooldown_active(cooldown_seconds)
    """

    def save_volume_gainers(
        self, symbols: List[str], count: int, fetched_at: Optional[str] = None
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def get_volume_gainers(self) -> Dict[str, Any]:
        raise NotImplementedError


class JSONStorage(StorageInterface):
    """File-based JSON storage implementation."""

    def __init__(self, data_file: Optional[Path] = None):
        self.data_file = data_file or Config.VOLUME_GAINERS_DATA_FILE
        self._ensure_storage_ready()

    def _ensure_storage_ready(self) -> None:
        """Ensure parent directory exists."""
        self.data_file.parent.mkdir(parents=True, exist_ok=True)

    def save_volume_gainers(
        self, symbols: List[str], count: Optional[int] = None, fetched_at: Optional[str] = None
    ) -> Dict[str, Any]:
        """Save latest volume gainers result to the JSON storage file.

        Args:
            symbols: List of extracted stock symbols.
            count: Number of symbols (defaults to len(symbols)).
            fetched_at: ISO timestamp string (defaults to current UTC time).

        Returns:
            Dict containing the persisted payload.
        """
        pid = os.getpid()
        abs_path = os.path.abspath(str(self.data_file))

        if fetched_at is None:
            fetched_at = datetime.now(timezone.utc).isoformat()

        if count is None:
            count = len(symbols)

        payload = {
            "source": "volume_gainers_spurts",
            "symbols": symbols,
            "count": count,
            "fetched_at": fetched_at,
        }

        # Check mtime before writing if file exists
        mtime_before = os.path.getmtime(abs_path) if os.path.exists(abs_path) else None
        size_before = os.path.getsize(abs_path) if os.path.exists(abs_path) else None

        try:
            # Atomic write pattern using temp file to avoid file corruption
            temp_file = self.data_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            temp_file.replace(self.data_file)

            mtime_after = os.path.getmtime(abs_path)
            size_after = os.path.getsize(abs_path)

            print(f"[STORAGE WRITE] PID={pid} | File={abs_path}")
            print(f"  * mtime before: {mtime_before} -> after: {mtime_after} (changed: {mtime_before != mtime_after})")
            print(f"  * size before : {size_before} bytes -> after: {size_after} bytes")
            print(f"  * symbols count: {count}")

            logger.info(
                "[STORAGE WRITE] PID=%d | File=%s | mtime=%s | size=%d bytes | count=%d",
                pid, abs_path, mtime_after, size_after, count
            )
            return payload
        except Exception as e:
            logger.error("Failed to write data to %s: %s", self.data_file, e)
            raise

    def get_volume_gainers(self) -> Dict[str, Any]:
        """Retrieve the latest volume gainers data from storage.

        Returns:
            Dict containing symbols, count, and fetched_at. If no data exists,
            returns a default empty structure with count=0 and fetched_at=None.
        """
        pid = os.getpid()
        abs_path = os.path.abspath(str(self.data_file))

        if not self.data_file.exists():
            print(f"[STORAGE READ] PID={pid} | File={abs_path} (NOT FOUND)")
            return {
                "source": "volume_gainers_spurts",
                "symbols": [],
                "count": 0,
                "fetched_at": None,
                "status": "empty",
            }

        try:
            mtime = os.path.getmtime(abs_path)
            size = os.path.getsize(abs_path)
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["status"] = "success" if data.get("symbols") else "empty"
                print(f"[STORAGE READ] PID={pid} | File={abs_path} | mtime={mtime} | size={size} bytes | symbols={len(data.get('symbols', []))}")
                return data
        except Exception as e:
            logger.error("Failed to read storage file %s: %s", self.data_file, e)
            return {
                "source": "volume_gainers_spurts",
                "symbols": [],
                "count": 0,
                "fetched_at": None,
                "status": "error",
                "error": str(e),
            }

    def get_last_fetched_timestamp(self) -> Optional[datetime]:
        """Parse and return the last fetched timestamp as a UTC datetime object."""
        data = self.get_volume_gainers()
        raw_ts = data.get("fetched_at")
        if not raw_ts:
            return None
        try:
            # Handle ISO string with or without Z/offset
            return datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except (ValueError, TypeError) as e:
            logger.warning("Could not parse fetched_at timestamp '%s': %s", raw_ts, e)
            return None

    def is_cooldown_active(self, cooldown_seconds: int) -> Tuple[bool, int]:
        """Check if a refresh request is within the anti-hammering cooldown window.

        Args:
            cooldown_seconds: Cooldown threshold in seconds.

        Returns:
            Tuple of (is_active: bool, remaining_seconds: int)
        """
        last_fetched = self.get_last_fetched_timestamp()
        if not last_fetched:
            return False, 0

        now = datetime.now(timezone.utc)
        elapsed = (now - last_fetched).total_seconds()

        if elapsed < cooldown_seconds:
            remaining = int(cooldown_seconds - elapsed)
            return True, remaining
        return False, 0


# Default global storage instance
storage = JSONStorage()


if __name__ == "__main__":
    print("=" * 70)
    print("STANDALONE STORAGE VERIFICATION TEST")
    print("=" * 70)
    resolved_path = os.path.abspath(str(storage.data_file))
    print(f"Storage absolute resolved path: {resolved_path}")
    print(f"Process ID (PID): {os.getpid()}")

    # 1. Check all volume_gainers.json files across workspace
    project_root = BASE_DIR
    print(f"\nScanning for all volume_gainers.json files under {project_root}...")
    found_files = []
    for root, dirs, files in os.walk(project_root):
        for fname in files:
            if fname == "volume_gainers.json":
                full_path = os.path.abspath(os.path.join(root, fname))
                mtime = os.path.getmtime(full_path)
                size = os.path.getsize(full_path)
                found_files.append((full_path, mtime, size))
                print(f"  -> Found: {full_path}")
                print(f"     Size: {size} bytes | Modified: {datetime.fromtimestamp(mtime)}")

    # 2. Test writing dummy value TESTSYMBOL123
    print("\nExecuting save_volume_gainers with dummy value ['TESTSYMBOL123']...")
    save_result = storage.save_volume_gainers(symbols=["TESTSYMBOL123"])
    print(f"Save returned: {save_result}")

    # 3. Test reading back in same script run
    print("\nExecuting get_volume_gainers...")
    read_result = storage.get_volume_gainers()
    print(f"Read returned: {read_result}")

    # 4. Verify match
    if read_result.get("symbols") == ["TESTSYMBOL123"]:
        print("\n[SUCCESS] TESTSYMBOL123 correctly persisted and loaded from disk!")
    else:
        print("\n[FAIL] Loaded symbols do not match TESTSYMBOL123!")
    print("=" * 70)
