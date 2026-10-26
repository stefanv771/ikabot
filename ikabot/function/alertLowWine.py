#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import time
import datetime
import traceback
from decimal import *
import json
import random

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
from ikabot.helpers.wineReserve import getWineReserve

getcontext().prec = 30


def restoreWineServing(session, city, manager):
    """Restore maximum serving only after wine arrives, and verify the level."""
    wine_available = Decimal(city["availableResources"][1])
    if wine_available <= 0:
        return False

    tavern = next((b for b in city.get("position", []) if isinstance(b, dict) and b.get("building") == "tavern"), None)
    if not tavern:
        return False

    data = manager._get_tavern_data(city, tavern, force_refresh=True)
    if not data or not data.get("action_code") or not data.get("consumption_values"):
        return False

    maximum = max(level for level, wine in data["consumption_values"])
    if maximum <= 0:
        return False

    # Cantitatea de vin pe ora necesara la nivelul maxim
    max_wine_per_hour = next((wine for lvl, wine in data["consumption_values"] if lvl == maximum), 0)

    # Daca vinul din depozit nu ajunge nici macar pentru 1 ora la maxim (ex: 150 ramas cand cere 300)
    if wine_available < max_wine_per_hour:
        return False

    if data.get("current_level") != maximum:
        manager._apply_level(city, tavern, maximum, data)

    confirmed = manager._get_tavern_data(city, tavern, force_refresh=True)
    return bool(confirmed and confirmed.get("current_level") == maximum)


def getMovementsFromHtml(session):
    """
    Extract fleet movements using the militaryAdvisor safely.
    """
    html = session.get()
    city_match = re.search(r"currentCityId:\s*(\d+),", html)
    action_match = re.search(r'actionRequest"?\s*[:=]\s*"([0-9a-f]+)"', html)

    if not city_match or not action_match:
        return []

    cityId = city_match.group(1)
    actionRequest = action_match.group(1)

    url = (
        f"view=militaryAdvisor&oldView=city&oldBackgroundView=city"
        f"&backgroundView=city&currentCityId={cityId}&actionRequest={actionRequest}&ajax=1"
    )
    resp = session.post(url)
    try:
        data = json.loads(resp, strict=False)
        for elem in data:
            if isinstance(elem, list) and len(elem) > 1 and isinstance(elem[1], list):
                for sub in elem[1]:
                    if isinstance(sub, dict) and "viewScriptParams" in sub:
                        return sub["viewScriptParams"].get("militaryAndFleetMovements", [])
    except Exception:
        pass

    return []


def getWineTransport(session, destinationCityId):
    """
    Check if there is an ongoing wine transport to the specified city.
    """
    movements = getMovementsFromHtml(session)
    destination_city_id_str = str(destinationCityId)

    for movement in movements:
        target = movement.get("target", {})
        resources = movement.get("resources", [])

        target_city_id = str(target.get("cityId"))

        if target_city_id == destination_city_id_str:
            wine_resource = next((res for res in resources if "wine" in res.get("cssClass", "")), None)
            if wine_resource:
                origin = movement.get("origin", {}).get("name", "Unknown")
                amount = wine_resource.get("amount", "Unknown")
                destination_name = target.get("name", "Unknown")
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
    info = f"\nI alert if the wine runs out in less than {hours:d} hours\n"
    setInfoSignal(session, info)
    was_alerted = {}
    message_log = []
    routes = []
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

            next_check[cityId] = time.monotonic() + 6 * 3600
            html = session.get(city_url + cityId)
            city = getCity(html)

            positions = [b.get("building") for b in city.get("position", []) if isinstance(b, dict)]
            if "tavern" not in positions:
                continue

            # Consumul citit include deja reducerea de la Teasc aplicata de joc
            consumption_per_hour = Decimal(getWineConsumptionPerHour(html))
            wine_available = Decimal(city["availableResources"][1])

            # Verificam daca exista transport activ spre acest oras
            active_transport = getWineTransport(session, cityId) if auto_transfer else None

            # Daca taverna este oprita (consum 0):
            if consumption_per_hour == 0:
                if active_transport:
                    next_check[cityId] = time.monotonic() + active_transport[1]
                    continue

                # Incercam sa pornim taverna la maxim cu ce vin avem disponibil
                if restoreWineServing(session, city, tavern_manager):
                    next_check[cityId] = time.monotonic() + 30
                    continue
                else:
                    # Stoc insuficient pentru o ora intreaga (chiar daca mai sunt resturi gen 150 < 300)
                    if auto_transfer:
                        pending_recovery.add(cityId)

            recovering = cityId in pending_recovery

            # Daca eram in recuperare si nava a sosit (nu mai e transport pe drum):
            if recovering and not active_transport:
                if restoreWineServing(session, city, tavern_manager):
                    pending_recovery.discard(cityId)
                    was_alerted[cityId] = False
                    message_log.append(f"Wine arrived in {city['name']}. Tavern serving confirmed at 100%.")
                    next_check[cityId] = time.monotonic() + 30
                    continue

            consumption_net = consumption_per_hour

            if consumption_net <= 0 and not recovering:
                was_alerted[cityId] = False
                continue

            consumption_per_sec = consumption_net / Decimal(3600)
            # Pentru orasele in curs de recuperare forțăm 0 secunde pentru a trimite transport
            seconds_left = Decimal(0) if recovering else wine_available / consumption_per_sec

            if seconds_left <= hours * 60 * 60:
                next_check[cityId] = time.monotonic() + (30 * 60 if auto_transfer else max(60, min(float(seconds_left), 6 * 3600)))
                if was_alerted[cityId] is False or auto_transfer:
                    time_left = daysHoursMinutes(int(seconds_left))
                    if not was_alerted[cityId]:
                        message_log.append(
                            f"In {city['name']} you have: {city['availableResources'][1]:,.0f} wine. "
                            f"Consumption: {float(consumption_per_hour):.2f} per hour.\n"
                            f"The wine will run out in {time_left}"
                        )

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
                        max_wine_available = 0

                        for donor_id, donor in cities.items():
                            if donor_id == cityId:
                                continue
                            wood_prod, luxury_prod, tradegood = getProductionPerHour(session, donor_id)

                            if tradegood != 1:  # Vin
                                continue

                            donor_html = session.get(city_url + donor_id)
                            donor_info = getCity(donor_html)

                            wine_available_donor = max(
                                0,
                                donor_info.get("availableResources", [0, 0, 0, 0, 0])[1] - getWineReserve(session, donor_info)
                            )
                            if wine_available_donor >= transfer_amount and wine_available_donor > max_wine_available:
                                donor_city_id = donor_id
                                donor_city = donor
                                max_wine_available = wine_available_donor

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

        # Calculul intervalului optim de somn
        calculated_sleep = max(1.0, min(next_check.values(), default=time.monotonic() + 6 * 3600) - time.monotonic())

        # Variație aleatorie (jitter) de +/- 5% daca somnul depaseste 2 minute
        if calculated_sleep > 120:
            jitter = random.uniform(-0.05, 0.05) * calculated_sleep
            sleep_duration = max(60.0, calculated_sleep + jitter)
        else:
            sleep_duration = calculated_sleep

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
