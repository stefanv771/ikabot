# ikabot/helpers/action_lock.py
import os
import time
import json
import random
from pathlib import Path

# Shared across all folders and processes on this machine
LOCK_FILE = Path.home() / ".ikabot_shared_action.json"

class GlobalActionLock:
    """Coordinates multi-account activity to prevent concurrent actions."""

    def __init__(self, username: str, min_delay_seconds: int = 15, max_jitter_seconds: int = 10):
        self.username = username
        self.min_delay = min_delay_seconds
        self.max_jitter = max_jitter_seconds

    def _read_state(self) -> dict:
        if not LOCK_FILE.exists():
            return {}
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_state(self, state: dict):
        try:
            with open(LOCK_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def wait_turn(self):
        """
        Ensures this account waits if another account recently executed an action.
        """
        while True:
            state = self._read_state()
            last_user = state.get("last_user")
            last_action_time = state.get("last_action_time", 0)
            now = time.time()

            # If the last action was by a DIFFERENT account:
            if last_user and last_user != self.username:
                elapsed = now - last_action_time
                required_delay = self.min_delay + random.uniform(2, self.max_jitter)

                if elapsed < required_delay:
                    wait_time = required_delay - elapsed
                    print(f"[*] Cooldown: {last_user} acted {int(elapsed)}s ago. {self.username} waiting {wait_time:.1f}s...")
                    time.sleep(wait_time)
                    continue  # Re-check in case another task acted in the meantime

            # Cooldown satisfied; update lock with our timestamp
            state["last_user"] = self.username
            state["last_action_time"] = time.time()
            self._write_state(state)
            break