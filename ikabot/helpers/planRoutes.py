#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import math
import random
import re
import time
from decimal import *

from ikabot.config import *
from ikabot.helpers.getJson import getCity
from ikabot.helpers.naval import *
from ikabot.helpers.varios import wait
from ikabot.helpers.pedirInfo import getShipCapacity


def extract_feedback_type(resp):
    """
    Safely searches for Ikariam's provideFeedback event without hardcoding array indices.
    Returns:
        int: 10 for success, 11 for busy/wait, or None if not found.
    """
    if not isinstance(resp, list):
        return None

    for entry in resp:
        if isinstance(entry, list) and len(entry) >= 2:
            if entry[0] == "provideFeedback" and isinstance(entry[1], list):
                for feedback in entry[1]:
                    if isinstance(feedback, dict) and "type" in feedback:
                        return feedback.get("type")
    return None


def sendGoods(session, originCityId, destinationCityId, islandId, ships, send, useFreighters=False):
    """
    Executes a single transport route safely with payload validation and retry limits.
    """
    attempts = 0
    max_attempts = 5

    while attempts < max_attempts:
        attempts += 1
        html = session.get()
        current_city = getCity(html)
        city = getCity(session.get(city_url + str(originCityId)))
        currId = current_city["id"]

        # 1. Switch active city context to the origin city
        switch_data = {
            "action": "header",
            "function": "changeCurrentCity",
            "actionRequest": actionRequest,
            "oldView": "city",
            "cityId": originCityId,
            "backgroundView": "city",
            "currentCityId": currId,
            "ajax": "1",
        }
        session.post(payloadPost=switch_data)

        # 2. Build transport payload
        data = {
            "action": "transportOperations",
            "function": "loadTransportersWithFreight",
            "destinationCityId": destinationCityId,
            "islandId": islandId,
            "oldView": "",
            "position": "",
            "avatar2Name": "",
            "city2Name": "",
            "type": "",
            "activeTab": "",
            "transportDisplayPrice": "0",
            "premiumTransporter": "0",
            "capacity": "5",
            "max_capacity": "5",
            "jetPropulsion": "0",
            "backgroundView": "city",
            "currentCityId": originCityId,
            "templateView": "transport",
            "currentTab": "tabSendTransporter",
            "actionRequest": actionRequest,
            "ajax": "1",
        }
        
        if useFreighters is False:
            data["transporters"] = ships
        else:
            data["usedFreightersShips"] = ships
            data["transporters"] = "0"

        for i in range(len(send)):
            if city["availableResources"][i] > 0:
                key = "cargo_resource" if i == 0 else "cargo_tradegood{:d}".format(i)
                data[key] = send[i]

        # 3. Submit as POST body (payloadPost)
        resp_text = session.post(payloadPost=data)
        
        try:
            resp = json.loads(resp_text, strict=False)
            fb_type = extract_feedback_type(resp)
        except Exception:
            fb_type = None

        if fb_type == 10:
            # Success: Goods dispatched!
            break
        elif fb_type == 11:
            # Ships are currently busy; wait for the closest fleet to return
            wait_seconds = getMinimumWaitingTime(session)
            wait(wait_seconds)
        else:
            # Unexpected response / temporary glitch; retry after short jitter
            time.sleep(random.randint(5, 12))


def executeRoutes(session, routes, useFreighters=False):
    """
    Executes all transport routes sequentially with space validation and random delays.
    """
    ship_capacity, freighter_capacity = getShipCapacity(session)
    active_capacity = freighter_capacity if useFreighters else ship_capacity

    for route in routes:
        (origin_city, destination_city, island_id, *toSend) = route
        destination_city_id = destination_city["id"]

        while sum(toSend) > 0:
            session.setStatus(
                f'Sending {toSend[0]}W, {toSend[1]}V, {toSend[2]}M, {toSend[3]}C, {toSend[4]}S | '
                f'{origin_city["name"]} ---> {destination_city["name"]}'
            )

            ships_available = waitForArrival(session, useFreighters)
            storageCapacityInShips = ships_available * active_capacity

            html = session.get(city_url + str(origin_city["id"]))
            origin_city = getCity(html)
            html = session.get(city_url + str(destination_city_id))
            destination_city = getCity(html)
            
            # Check if target is our own city or a foreign player's city
            foreign = str(destination_city["id"]) != str(destination_city_id)
            if foreign is False:
                storageCapacityInCity = destination_city.get("freeSpaceForResources", [float("inf")] * len(toSend))

            send = []
            for i in range(len(toSend)):
                if foreign is False:
                    min_val = min(
                        origin_city["availableResources"][i],
                        toSend[i],
                        storageCapacityInShips,
                        storageCapacityInCity[i],
                    )
                else:
                    min_val = min(
                        origin_city["availableResources"][i],
                        toSend[i],
                        storageCapacityInShips,
                    )
                send.append(min_val)
                storageCapacityInShips -= send[i]
                toSend[i] -= send[i]

            resources_to_send = sum(send)
            if resources_to_send == 0:
                # No storage space or resources available right now
                # Wait ~1h (randomized 45-75 min) and try again
                wait(random.randint(45 * 60, 75 * 60))
                continue

            available_ships = int(math.ceil(Decimal(resources_to_send) / Decimal(active_capacity)))

            sendGoods(
                session,
                origin_city["id"],
                destination_city_id,
                island_id,
                available_ships,
                send,
                useFreighters,
            )
            # Randomized pause between shipments to prevent bot flag
            time.sleep(random.randint(5, 15))


def splitCargoBetweenFleets(session, toSend):
    """
    Splits cargo between faster trade ships first and freighters for the remainder.
    """
    tradeShipCargo = [0] * len(toSend)
    freighterCargo = [0] * len(toSend)

    if getAvailableFreighters(session) == 0:
        return list(toSend), freighterCargo

    ship_capacity, _ = getShipCapacity(session)
    tradeShipSpace = getAvailableShips(session) * ship_capacity
    if tradeShipSpace == 0:
        return tradeShipCargo, list(toSend)

    for i in range(len(toSend)):
        tradeShipCargo[i] = min(toSend[i], tradeShipSpace)
        tradeShipSpace -= tradeShipCargo[i]
        freighterCargo[i] = toSend[i] - tradeShipCargo[i]

    return tradeShipCargo, freighterCargo


def get_random_wait_time():
    """Returns random jitter (0 to 60 seconds)."""
    return random.randint(0, 20) * 3


def getMinimumWaitingTime(session):
    """
    Returns the seconds until the nearest returning fleet arrives.
    """
    html = session.get()
    
    # Resilient currentCityId extraction
    match = re.search(r"currentCityId:\s*(\d+)", html)
    if match:
        idCiudad = match.group(1)
    else:
        idCiudad = getCity(html).get("id", "0")

    url = (
        f"view=militaryAdvisor&oldView=city&oldBackgroundView=city&backgroundView=city&"
        f"currentCityId={idCiudad}&actionRequest={actionRequest}&ajax=1"
    )
    posted = session.post(url)
    
    try:
        postdata = json.loads(posted, strict=False)
        militaryMovements = postdata[1][1][2]["viewScriptParams"]["militaryAndFleetMovements"]
        current_time = int(postdata[0][1]["time"])
    except Exception:
        return get_random_wait_time() + 15

    delivered_times = []
    for militaryMovement in [mv for mv in militaryMovements if mv.get("isOwnArmyOrFleet")]:
        remaining_time = int(militaryMovement.get("eventTime", 0)) - current_time
        if remaining_time > 0:
            delivered_times.append(remaining_time)

    if delivered_times:
        return min(delivered_times) + get_random_wait_time()
    else:
        return 0


def waitForArrival(session, useFreighters=False):
    """
    Blocks execution until at least one requested ship is free.
    """
    get_ships = getAvailableFreighters if useFreighters else getAvailableShips
    available_ships = get_ships(session)

    while available_ships == 0:
        minimum_waiting_time_for_ship = getMinimumWaitingTime(session)
        # Avoid 0-second spin lock
        if minimum_waiting_time_for_ship <= 0:
            minimum_waiting_time_for_ship = random.randint(15, 30)
        wait(minimum_waiting_time_for_ship)
        available_ships = get_ships(session)

    return available_ships