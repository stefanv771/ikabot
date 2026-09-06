#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import random
import threading
import time
import traceback

from ikabot.function.vacationMode import activateVacationMode
from ikabot.helpers.botComm import *
from ikabot.helpers.gui import enter
from ikabot.helpers.process import set_child_mode
from ikabot.helpers.signals import setInfoSignal
from ikabot.helpers.varios import daysHoursMinutes


def _jittered_interval_seconds(base_minutes, min_ratio=0.8, max_ratio=1.2):
    """
    Parameters
    ----------
    base_minutes : int
        the interval the user configured, used as the average
    min_ratio : float
    max_ratio : float

    Returns
    -------
    seconds : int
        a randomized interval (in seconds), gaussian-distributed around
        base_minutes, clipped to [base*min_ratio, base*max_ratio] so it
        never drifts more than ~20% from what the user chose, and never
        below 3 minutes (matching the minimum enforced at setup).
    """
    base_seconds = base_minutes * 60
    std_dev = base_seconds * 0.08  # keeps most draws well inside +-20%
    jittered = random.gauss(base_seconds, std_dev)
    lower = base_seconds * min_ratio
    upper = base_seconds * max_ratio
    jittered = max(lower, min(upper, jittered))
    return max(180, int(jittered))


def alertAttacks(session, event, stdin_fd, predetermined_input):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    event : multiprocessing.Event
    stdin_fd: int
    predetermined_input : multiprocessing.managers.SyncManager.list
    """
    sys.stdin = os.fdopen(stdin_fd)
    config.predetermined_input = predetermined_input
    try:
        if checkTelegramData(session) is False:
            event.set()
            return

        banner()
        default = 20
        minutes = read(
            msg=
                "How often should I search for attacks?(min:3, default: {:d}): ".format(default),
            min=3,
            default=default,
        )
        # min_units = read(msg=_('Attacks with less than how many units should be ignored? (default: 0): '), digit=True, default=0)
        print("I will check for attacks every {:d} minutes".format(minutes))
        enter()
    except KeyboardInterrupt:
        event.set()
        return

    set_child_mode(session)
    event.set()

    info = "\nI check for attacks every {:d} minutes\n".format(minutes)
    setInfoSignal(session, info)
    try:
        do_it(session, minutes)
    except Exception as e:
        msg = "Error in:\n{}\nCause:\n{}".format(info, traceback.format_exc())
        sendToBot(session, msg)
    finally:
        session.logout()


def respondToAttack(session):
    """
    Parameters
    ---------
    session : ikabot.web.session.Session
    """

    while True:
        start_time = time.time()
        responses = getUserResponse(session)
        for response in responses:
            rta = re.search(r"(\d+):?\s*(\d+)", response)
            if rta is None:
                continue

            pid = int(rta.group(1))
            action = int(rta.group(2))

            if pid != os.getpid():
                continue

            if action == 1:
                activateVacationMode(session)
            else:
                sendToBot(session, "Invalid command: {:d}".format(action))

        elapsed = time.time() - start_time
        time.sleep(max(0, 60 * 3 - elapsed))


def do_it(session, minutes):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    minutes : int
    """

    thread = threading.Thread(target=respondToAttack, args=(session,))
    thread.start()

    knownAttacks = []
    while True:
        start_time = time.time()
        currentAttacks = []
        try:
            html = session.get()
            city_id = re.search(r"currentCityId:\s(\d+),", html).group(1)
            url = "view=militaryAdvisor&oldView=city&oldBackgroundView=city&backgroundView=city&currentCityId={}&actionRequest={}&ajax=1".format(
                city_id, actionRequest
            )
            movements_response = session.post(url)
            postdata = json.loads(movements_response, strict=False)
            militaryMovements = postdata[1][1][2]["viewScriptParams"][
                "militaryAndFleetMovements"
            ]
            timeNow = int(postdata[0][1]["time"])

            for militaryMovement in [
                mov for mov in militaryMovements if mov["isHostile"]
            ]:
                event_id = militaryMovement["event"]["id"]
                currentAttacks.append(event_id)
                if event_id not in knownAttacks:
                    knownAttacks.append(event_id)

                    missionText = militaryMovement["event"]["missionText"]
                    origin = militaryMovement["origin"]
                    target = militaryMovement["target"]
                    amountTroops = militaryMovement["army"]["amount"]
                    amountFleets = militaryMovement["fleet"]["amount"]
                    timeLeft = int(militaryMovement["eventTime"]) - timeNow

                    msg = "-- ALERT --\n"
                    msg += missionText + "\n"
                    msg += "from the city {} of {}\n".format(
                        origin["name"], origin["avatarName"]
                    )
                    msg += "a {}\n".format(target["name"])
                    msg += "{} units\n".format(amountTroops)
                    msg += "{} fleet\n".format(amountFleets)
                    msg += "arrival in: {}\n".format(daysHoursMinutes(timeLeft))
                    msg += "If you want to put the account in vacation mode send:\n"
                    msg += "{:d}:1".format(os.getpid())
                    sendToBot(session, msg)

        except Exception as e:
            info = "\nI check for attacks every {:d} minutes\n".format(minutes)
            msg = "Error in:\n{}\nCause:\n{}".format(info, traceback.format_exc())
            sendToBot(session, msg)

        for event_id in list(knownAttacks):
            if event_id not in currentAttacks:
                knownAttacks.remove(event_id)

        elapsed = time.time() - start_time
        next_check_seconds = max(0, _jittered_interval_seconds(minutes) - elapsed)
        next_check_time = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + next_check_seconds)
        )
        session.setStatus(f"Next attack check at {next_check_time}")
        time.sleep(next_check_seconds)
