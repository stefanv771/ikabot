#! /usr/bin/env python3
# -*- coding: utf-8 -*-

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
from ikabot.helpers.decorators import configurator, task
from ikabot.helpers.resources import getWineConsumptionPerHour, getAvailableResources, getProductionPerHour
from ikabot.helpers.signals import setInfoSignal
from ikabot.helpers.varios import daysHoursMinutes
from ikabot.helpers.planRoutes import *
from ikabot.function.tavernManager import TavernManager

getcontext().prec = 30


def restoreWineServing(session, city, manager):
    """Restore maximum serving only after wine arrives, and verify the level."""
    if Decimal(city["availableResources"][1]) <= 0:
        return False
    tavern = next(b for b in city["position"] if b["building"] == "tavern")
    data = manager._get_tavern_data(city, tavern, force_refresh=True)
    if not data or not data["action_code"] or not data["consumption_values"]:
        return False
    maximum = max(level for level, wine in data["consumption_values"])
    if maximum <= 0:
        return False
    if data["current_level"] != maximum:
        manager._apply_level(city, tavern, maximum, data)
    confirmed = manager._get_tavern_data(city, tavern, force_refresh=True)
    return bool(confirmed and confirmed["current_level"] == maximum)

def getMovementsFromHtml(session):
    """
    Extract fleet movements using the same approach as See movements.
    Parameters
    ----------
    session : ikabot.web.session.Session
    Returns
    -------
    list
        A list of movements.
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

def getWineTransport(session, destinationCityId):
    """
    Check if there is an ongoing wine transport to the specified city.
    Parameters
    ----------
    session : ikabot.web.session.Session
    destinationCityId : str
    Returns
    -------
    bool
    """
    movements = getMovementsFromHtml(session)
    for movement in movements:
        target = movement.get("target", {})
        resources = movement.get("resources", [])

        # Convert both IDs to the same type for comparison
        target_city_id = str(target.get("cityId"))
        destination_city_id_str = str(destinationCityId)

        # Check if the target city matches and the transport includes wine
        if target_city_id == destination_city_id_str:
            wine_resource = next((res for res in resources if res.get("cssClass") == "resource_icon wine"), None)
            if wine_resource:
                origin = movement.get("origin", {}).get("name", "Unknown")
                amount = wine_resource.get("amount", "Unknown")
                destination_name = movement.get("target", {}).get("name", "Unknown")
                try:
                    delay = max(60, float(movement["eventTime"]) - time.time() + 60)
                except (KeyError, TypeError, ValueError):
                    delay = 30 * 60
                return (f"Active wine transport: {amount} wine from {origin} to {destination_name}.", delay)

    return None


def isWineTransportInProgress(session, destinationCityId):
    transport = getWineTransport(session, destinationCityId)
    return transport[0] if transport else None


def nextWineCheck(seconds_left, hours):
    """Time until the alert threshold, capped for changes made outside this task."""
    return max(60, min(float(seconds_left) - hours * 3600, 6 * 3600))

@task("alertLowWine")
def do_it(session, hours, auto_transfer, transfer_amount):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    hours : int
    auto_transfer : bool
    transfer_amount : int
    """
    info = "\nI alert if the wine runs out in less than {:d} hours\n".format(hours)
    setInfoSignal(session, info)
    was_alerted = {}
    message_log = []
    routes = []  # Store all routes for batch execution
    pending_recovery = set()
    next_check = {}
    tavern_manager = TavernManager(session)
    
    while True:
        ids, cities = getIdsOfCities(session)
        next_check = {key: value for key, value in next_check.items() if key in cities}
        pending_recovery.intersection_update(cities)

        for cityId in cities:
            if cityId not in was_alerted:
                was_alerted[cityId] = False

        for cityId in cities:
            if time.monotonic() < next_check.get(cityId, 0):
                continue
            # Default for cities without consumption; overridden by threshold/arrival.
            next_check[cityId] = time.monotonic() + 6 * 3600
            html = session.get(city_url + cityId)
            city = getCity(html)

            if "tavern" not in [building["building"] for building in city["position"]]:
                continue

            consumption_per_hour = getWineConsumptionPerHour(html)

            # Remember exhausted cities until wine arrives and serving is restored.
            if auto_transfer and Decimal(city["availableResources"][1]) <= 0 and consumption_per_hour == 0:
                pending_recovery.add(cityId)
            recovering = cityId in pending_recovery
            if recovering and Decimal(city["availableResources"][1]) > 0:
                if restoreWineServing(session, city, tavern_manager):
                    pending_recovery.remove(cityId)
                    was_alerted[cityId] = False
                    message_log.append(f"Wine arrived in {city['name']}. Tavern serving confirmed at 100%.")
                next_check[cityId] = time.monotonic() + 60
                # Refresh consumption on the next pass after changing the tavern.
                continue

            # Determine Wine Press reduction
            wine_press_level = 0
            for building in city["position"]:
                if building.get("building") == "vineyard":
                    wine_press_level = building.get("level", 0)
                    break

            # Apply reduction to wine consumption
            reduction_factor = Decimal(1 - (wine_press_level / 100))
            consumption_per_hour *= reduction_factor

            wine_available = Decimal(city["availableResources"][1])

            consumption_net = Decimal(consumption_per_hour)

            if consumption_net <= 0 and not recovering:
                was_alerted[cityId] = False
                continue

            consumption_per_seg = consumption_net / Decimal(3600)
            seconds_left = Decimal(0) if recovering else wine_available / consumption_per_seg

            if seconds_left <= hours * 60 * 60:
                next_check[cityId] = time.monotonic() + (30 * 60 if auto_transfer else max(60, min(float(seconds_left), 6 * 3600)))
                if was_alerted[cityId] is False or auto_transfer:
                    time_left = daysHoursMinutes(seconds_left)
                    if not was_alerted[cityId]:
                        message_log.append(f"In {city['name']} you have: {city['availableResources'][1]:,.0f} wine. Consumption: {consumption_per_hour:.2f} per hour.\nThe wine will run out in {time_left}")

                    if auto_transfer:
                        transport = getWineTransport(session, cityId)
                        if transport:
                            next_check[cityId] = time.monotonic() + transport[1]
                            if not was_alerted[cityId]:
                                message_log.append(transport[0])
                            was_alerted[cityId] = True
                            continue

                        donor_city_id = None
                        donor_city = None

                        # Find the donor city among wine-producing cities with the most wine available
                        max_wine_available = 0
                        for donor_id, donor in cities.items():
                            if donor_id == cityId:
                                continue
                            wood_prod, luxury_prod, tradegood = getProductionPerHour(session, donor_id)
                                
                            if tradegood != 1:  # Skip if not wine-producing
                                continue

                            donor_html = session.get(city_url + donor_id)
                            donor_info = getCity(donor_html)

                            # Check if the city has enough wine to send
                            wine_available = donor_info.get("availableResources", [0, 0, 0, 0, 0])[1]
                            if wine_available >= transfer_amount and wine_available > max_wine_available:
                                donor_city_id = donor_id
                                donor_city = donor
                                max_wine_available = wine_available

                        if donor_city_id:
                            routes.append((
                                donor_city,  # Origin
                                city,  # Destination
                                city["islandId"],  # Island ID
                                0,  # Wood
                                transfer_amount,  # Wine
                                0,  # Marble
                                0,  # Crystal
                                0,  # Sulfur
                            ))
                            
                            message_log.append(f"Will transfer {transfer_amount:,d} from {donor_city['name']}.")

                        else:
                            message_log.append(f"No city has sufficient wine to transfer to {city['name']}.")

                    was_alerted[cityId] = True
            else:
                was_alerted[cityId] = False
                next_check[cityId] = time.monotonic() + nextWineCheck(seconds_left, hours)

        if message_log:
            
            sendToBot(session, "\n".join(message_log))
            message_log.clear()

        if routes:
            destinations = [route[1]["id"] for route in routes]
            executeRoutes(session, routes, useFreighters=False)
            routes.clear()
            for destination in destinations:
                transport = getWineTransport(session, destination)
                next_check[destination] = time.monotonic() + (transport[1] if transport else 30 * 60)
        # Each city has its own deadline, so another city's event does not poll it early.
        sleep_duration = max(1, min(next_check.values(), default=time.monotonic() + 6 * 3600) - time.monotonic())
        next_wake = datetime.datetime.now() + datetime.timedelta(seconds=sleep_duration)
        session.setStatus(f"Next wine check at {next_wake.strftime('%H:%M')}")
        time.sleep(sleep_duration)
@configurator
def alertLowWine(session, event, stdin_fd, predetermined_input):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    event : multiprocessing.Event
    stdin_fd: int
    predetermined_input : multiprocessing.managers.SyncManager.list
    """
    if checkTelegramData(session) is False:
        return None
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
    return {"session": session, "hours": hours, "auto_transfer": auto_transfer, "transfer_amount": transfer_amount}

