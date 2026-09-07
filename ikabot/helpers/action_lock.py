# ikabot/helpers/action_lock.py
import os
import time
import json
import random
from pathlib import Path


class GlobalActionLock:
    """
    Coordinates multi-account activity per server to prevent concurrent
    mutating requests (POST actions) from the same IP address.
    """

    def __init__(self, username: str, server: str, min_delay_seconds: int = 15, max_jitter_seconds: int = 10):
        self.username = username
        self.server = server
        self.min_delay = min_delay_seconds
        self.max_jitter = max_jitter_seconds
        # Dedicated lock file per game world server (e.g. ~/.ikabot_lock_s598-en.json)
        self.lock_file = Path.home() / f".ikabot_lock_{self.server}.json"

    def _read_state(self) -> dict:
        if not self.lock_file.exists():
            return {}
        try:
            with open(self.lock_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_state(self, state: dict):
        try:
            with open(self.lock_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def wait_turn(self):
        """
        Ensures accounts sharing the same game world wait their turn with humanized pauses.
        """
        while True:
            state = self._read_state()
            last_user = state.get("last_user")
            last_action_time = state.get("last_action_time", 0)
            now = time.time()

            # If the last action was executed by a DIFFERENT account on THIS server:
            if last_user and last_user != self.username:
                elapsed = now - last_action_time
                required_delay = self.min_delay + random.uniform(2, self.max_jitter)

                if elapsed < required_delay:
                    wait_time = required_delay - elapsed
                    print(f"[*] Cooldown on {self.server}: {last_user} acted {int(elapsed)}s ago. {self.username} waiting {wait_time:.1f}s...")
                    time.sleep(wait_time)
                    continue  # Re-check state after sleeping

            # Turn satisfied; record this account's timestamp
            state["last_user"] = self.username
            state["last_action_time"] = time.time()
            self._write_state(state)
            break