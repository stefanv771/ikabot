import sys
import time
from ikabot.helpers.crossAccountLock import (
    acquire_activity_lock,
    release_activity_lock,
)

username = sys.argv[1] if len(sys.argv) > 1 else "account"

def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {username}: {message}", flush=True)

while True:
    log("waiting for lock")

    acquired = acquire_activity_lock(
        username,
        timeout=60,
        poll_interval=1,
    )
    log(f"acquired={acquired}")

    if acquired:
        try:
            log("working")
            time.sleep(5)
            log("work finished")
        finally:
            log("releasing; cooldown starts")
            release_activity_lock()
            log("released")

    time.sleep(5)
