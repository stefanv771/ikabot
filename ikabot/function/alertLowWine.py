#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import time
import datetime
import traceback
from decimal import *
import json

from ikabot.config import *
from ikabot.helpers.botComm import *
from ikabot.helpers.getJson import getCity
from ikabot.helpers.gui import *
from ikabot.helpers.pedirInfo import getIdsOfCities
from ikabot.helpers.process import set_child_mode
from ikabot.helpers.resources import getWineConsumptionPerHour, getAvailableResources, getProductionPerHour
from ikabot.helpers.signals import setInfoSignal
from ikabot.helpers.varios import daysHoursMinutes
from ikabot.helpers.planRoutes import *

getcontext().prec = 30


def alertLowWine(session, event, stdin_fd, predetermined_input):
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
        hours = read(
            msg=(
                "How many hours should be left until the wine runs out in a city so that it's alerted? : "
            ),
            min=1,
        )
        auto_transfer = read(msg=("Would you like to automatically transfer wine if necessary? (y/n) : ")).strip().lower()
        
        if auto_transfer in ["y", "yes"]:
            auto_transfer = True
            transfer_amount = read(msg=("How much wine should be sent automatically? : "), min=1)
        else:
            auto_transfer = False
            transfer_amount = 0
        print("It will be alerted when the wine runs out in less than {:d} hours in any city, and {:,d} wine will be transferred if necessary.".format(hours, transfer_amount))

        enter()
    except KeyboardInterrupt:
        event.set()
        return

    set_child_mode(session)
    event.set()

    info = ("\nI alert if the wine runs out in less than {:d} hours\n".format(hours))
    setInfoSignal(session, info)
    try:
        do_it(session, hours, auto_transfer, transfer_amount)
    except Exception as e:
        msg = f"Error in:\n{info}\nCause:\n{traceback.format_exc()}"
        sendToBot(session, msg)
    finally:
        session.logout()


def getMovementsFromHtml(session):
    """
    Extract fleet movements using the militaryAdvisor.
    """
    html = session.get()
    cityId = re.search(r"currentCityId:\s*(\d+),", html).group(1)
    url = "view=militaryAdvisor&oldView=city&oldBackgroundView=city&backgroundView=city&currentCityId={}&actionRequest={}&ajax=1".format(
        cityId, actionRequest
    )
    resp = session.post(url)
    resp = json.loads(resp, strict=False)
    movements = resp[1][1][2]["viewScriptParams"]["militaryAndFleetMovements"]
    return movements


def isWineTransportInProgress(session, destinationCityId):
    """
    Check if there is an ongoing wine transport to the specified city.
    """
    movements = getMovementsFromHtml(session)
    for movement in movements:
        target = movement.get("target", {})
        resources = movement.get("resources", [])

        target_city_id = str(target.get("cityId"))
        destination_city_id_str = str(destinationCityId)

        if target_city_id == destination_city_id_str:
            wine_resource = next((res for res in resources if res.get("cssClass") == "resource_icon wine"), None)
            if wine_resource:
                origin = movement.get("origin", {}).get("name", "Unknown")
                amount = wine_resource.get("amount", "Unknown")
                destination_name = movement.get("target", {}).get("name", "Unknown")
                return f"Active wine transport: {amount} wine from {origin} to {destination_name}."

    return None


def do_it(session, hours, auto_transfer, transfer_amount):
    """
    Main background loop with dynamic next-event sleep scheduling.
    """
    was_alerted = {}
    message_log = []
    routes = []
    
    # Sleep clamps
    MIN_SLEEP = 10 * 60      # Minimum 10 minutes (prevents spamming on low wine)
    MAX_SLEEP = 6 * 60 * 60  # Maximum 6 hours (sanity check for manual changes)

    while True:
        ids, cities = getIdsOfCities(session)

        for cityId in cities:
            if cityId not in was_alerted:
                was_alerted[cityId] = False

        seconds_until_next_event = []

        for cityId in cities:
            html = session.get(city_url + cityId)
            city = getCity(html)

            # Skip cities without a tavern
            if "tavern" not in [building["building"] for building in city["position"]]:
                continue

            consumption_per_hour = getWineConsumptionPerHour(html)

            # Determine Wine Press / Vineyard reduction
            wine_press_level = 0
            for building in city["position"]:
                if building.get("building") == "vineyard":
                    wine_press_level = building.get("level", 0)
                    break

            reduction_factor = Decimal(1 - (wine_press_level / 100))
            consumption_per_hour *= reduction_factor

            wine_available = Decimal(city["availableResources"][1])
            consumption_net = Decimal(consumption_per_hour)

            if consumption_net <= 0:
                was_alerted[cityId] = False
                continue

            consumption_per_seg = consumption_net / Decimal(3600)
            seconds_left = wine_available / consumption_per_seg
            threshold_seconds = Decimal(hours * 3600)

            # Case 1: Wine is at or below the alert threshold
            if seconds_left <= threshold_seconds:
                if not was_alerted[cityId]:
                    time_left = daysHoursMinutes(seconds_left)
                    message_log.append(
                        f"In {city['name']} you have: {city['availableResources'][1]:,.0f} wine. "
                        f"Consumption: {consumption_per_hour:.2f}/h.\n"
                        f"The wine will run out in {time_left}."
                    )

                    if auto_transfer:
                        transport_status = isWineTransportInProgress(session, cityId)
                        if transport_status:
                            message_log.append(transport_status)
                            message_log.append(f"Transport already en route to {city['name']}. No additional transport initiated.")
                            was_alerted[cityId] = True
                            seconds_until_next_event.append(30 * 60)
                            continue

                        # Find the donor city with the most surplus wine
                        donor_city_id = None
                        donor_city = None
                        max_wine_available = 0

                        for donor_id, donor in cities.items():
                            if donor_id == cityId:
                                continue

                            wood_prod, luxury_prod, tradegood = getProductionPerHour(session, donor_id)
                            if tradegood != 1:  # 1 = Wine producing
                                continue

                            donor_html = session.get(city_url + donor_id)
                            donor_info = getCity(donor_html)

                            donor_wine = donor_info.get("availableResources", [0, 0, 0, 0, 0])[1]
                            if donor_wine >= transfer_amount and donor_wine > max_wine_available:
                                donor_city_id = donor_id
                                donor_city = donor
                                max_wine_available = donor_wine

                        if donor_city_id:
                            routes.append((
                                donor_city,
                                city,
                                city["islandId"],
                                0,
                                transfer_amount,
                                0,
                                0,
                                0,
                            ))
                            message_log.append(f"Will transfer {transfer_amount:,d} wine from {donor_city['name']}.")
                            seconds_until_next_event.append(30 * 60)
                        else:
                            message_log.append(f"No donor city has {transfer_amount:,d} wine available for {city['name']}.")
                            seconds_until_next_event.append(min(float(seconds_left), 3600))

                    was_alerted[cityId] = True
                else:
                    # Already alerted: next milestone is total depletion
                    if seconds_left > 0:
                        seconds_until_next_event.append(float(seconds_left))
            else:
                # Case 2: Wine is safe -> calculate time until it reaches threshold
                was_alerted[cityId] = False
                time_to_threshold = float(seconds_left - threshold_seconds)
                seconds_until_next_event.append(time_to_threshold)

        # Send Telegram notifications
        if message_log:
            sendToBot(session, "\n".join(message_log))
            message_log.clear()

        # Dispatch transport routes
        if routes:
            executeRoutes(session, routes, useFreighters=False)
            routes.clear()
            seconds_until_next_event.append(20 * 60)

        # Calculate optimal sleep time
        if seconds_until_next_event:
            calculated_sleep = min(seconds_until_next_event)
        else:
            calculated_sleep = MAX_SLEEP

        # Clamp between MIN_SLEEP (10m) and MAX_SLEEP (6h)
        sleep_duration = max(MIN_SLEEP, min(int(calculated_sleep), MAX_SLEEP))

        next_wake = datetime.datetime.now() + datetime.timedelta(seconds=sleep_duration)
        session.setStatus(f"Wine OK. Next check in {daysHoursMinutes(sleep_duration)} (at {next_wake.strftime('%H:%M')})")

        time.sleep(sleep_duration)
