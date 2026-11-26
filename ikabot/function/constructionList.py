#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import html
import json
import math
import random
import re
import threading
import time
import traceback
from decimal import *
from urllib.parse import parse_qs, urlparse

import requests
from functools import cache

from ikabot.config import *
from ikabot.helpers.botComm import *
from ikabot.helpers.getJson import getCity
from ikabot.helpers.gui import *
from ikabot.helpers.pedirInfo import *
from ikabot.helpers.planRoutes import *
from ikabot.helpers.resources import getAvailableResources
from ikabot.helpers.signals import setInfoSignal
from ikabot.helpers.varios import *
from ikabot.web.session import normal_get
from ikabot.helpers.decorators import configurator, task
from ikabot.helpers.activityCoordinator import activity
from ikabot.helpers.wineReserve import getWineReserve, WINE_INDEX, WINE_RESERVE, WINE_RESERVE_HOURS

# Planning margin only; the live check remains authoritative after transports.
WINE_TRANSPORT_BUFFER_HOURS = 1


def getResourcesOverCapacity(city, resourcesNeeded):
    capacity = city.get("storageCapacity")
    if capacity is None:
        return []
    listed = city.get("resourcesListedForSale", [0] * len(materials_names))
    return [
        (materials_names[i], needed, max(0, capacity - listed[i]))
        for i, needed in enumerate(resourcesNeeded)
        if needed > max(0, capacity - listed[i])
    ]


def _constructionRequirements(session, city, costs, waiting_seconds=0):
    required = list(costs)
    if required[WINE_INDEX] > 0:
        required[WINE_INDEX] += getWineReserve(session, city, waiting_seconds)
    return required


def _waitForWineReserve(session, city, building, wait_for_resources):
    """
    Asigura ca avem resursele pentru constructie. Daca cladirea cere vin (ex: Palat),
    asteapta transporturile daca sunt pe drum, dar daca transporturile au sosit si avem
    vinul necesar pentru costul cladirii, permite upgrade-ul chiar daca taverna a consumat
    o parte din rezerva in timpul transportului.
    """
    notified = False
    while True:
        costs = getResourcesNeeded(session, city, dict(building), building["level"], building["level"] + 1)
        if any(cost < 0 for cost in costs):
            return None

        wine_cost = costs[WINE_INDEX]
        wine_available = city["availableResources"][WINE_INDEX]

        # Daca cladirea nu consuma vin, putem continua direct
        if wine_cost == 0:
            return city

        wine_reserve = getWineReserve(session, city, 0)
        full_required = wine_cost + wine_reserve

        # Cazul ideal: avem vin pentru constructie SI toata rezerva intacta
        if wine_available >= full_required:
            return city

        # Daca avem destul vin pentru costul cladirii
        if wine_available >= wine_cost:
            seconds = getMinimumWaitingTime(session) if wait_for_resources else 0
            # Daca inca mai vin nave pe drum, asteptam sa soseasca tot vinul
            if seconds > 0:
                wait(seconds + 5)
                city = getCity(session.get(city_url + str(city["id"])))
                building = city["position"][building["position"]]
                continue
            else:
                # Toate navele au sosit; chiar daca taverna a baut o parte din rezerva,
                # avem vinul pentru cladire -> CONSTRUIM!
                return city

        # Daca NU avem destul vin nici macar pentru costul de constructie:
        if not notified:
            sendToBot(session, f"{city['cityName']}: construction paused. Need {wine_cost:,} wine for {building['name']}, have {wine_available:,}.")
            notified = True

        if not wait_for_resources:
            return None

        seconds = getMinimumWaitingTime(session)
        if seconds <= 0:
            sendToBot(session, f"{city['cityName']}: no transport detected; construction of {building['name']} stopped due to insufficient wine.")
            return None

        wait(seconds + 5)
        city = getCity(session.get(city_url + str(city["id"])))
        building = city["position"][building["position"]]


def _parseConstructionTime(row):
    cells = re.findall(r'<td class="costs">(.*?)</td>', row, re.DOTALL)
    if not cells:
        return None

    cell = cells[-1]
    match = re.search(r'data-(?:seconds|duration|time)=["\'](\d+)', cell)
    if match:
        return int(match.group(1))

    values = re.findall(r'title=["\']([^"\']+)', cell)
    values.append(re.sub(r"<[^>]+>", " ", html.unescape(cell)))
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    for value in values:
        parts = re.findall(r"(\d+)\s*([dhms])", value.lower())
        if parts:
            return sum(int(amount) * units[unit] for amount, unit in parts)
    return None


def _formatRemainingTime(seconds):
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    values = ((days, "D"), (hours, "H"), (minutes, "M"), (seconds, "S"))
    return " ".join("{}{}".format(value, unit) for value, unit in values if value) or "0S"


def _findHtml(value, element_id):
    if isinstance(value, str):
        return value if element_id in value and "ambrosiaIcon" in value else None
    if isinstance(value, list):
        for item in value:
            result = _findHtml(item, element_id)
            if result is not None:
                return result
    if isinstance(value, dict):
        for item in value.values():
            result = _findHtml(item, element_id)
            if result is not None:
                return result
    return None


def _getFreeSpeedupParams(response, city_id, position):
    try:
        popup = _findHtml(json.loads(response, strict=False), "js_buildingSpeedupActivateBtn")
    except (TypeError, ValueError):
        popup = response if isinstance(response, str) else None
    if popup is None:
        return None

    buttons = re.findall(
        r'<a\b(?=[^>]*\bid=["\']js_buildingSpeedupActivateBtn["\'])[^>]*>.*?</a>',
        popup,
        re.DOTALL,
    )
    if len(buttons) != 1:
        return None
    button = buttons[0]

    span_pattern = r'<span\b(?=[^>]*\bclass=["\'][^"\']*\bambrosiaIcon\b[^"\']*["\'])[^>]*>'
    ambrosia_spans = re.findall(span_pattern, button)
    costs = re.findall(span_pattern + r'\s*(\d+)\s*</span>', button)
    if len(ambrosia_spans) != 1 or len(costs) != 1 or costs[0] != "0":
        return None

    href = re.search(r'href=["\']([^"\']+)', button)
    if href is None:
        return None

    query = parse_qs(urlparse(html.unescape(href.group(1))).query)
    required = {
        "action": "Premium",
        "function": "buildingSpeedup",
        "cityId": str(city_id),
        "position": str(position),
    }
    if any(query.get(key) != [value] for key, value in required.items()):
        return None
    if query.get("currentCityId", [str(city_id)]) != [str(city_id)]:
        return None
    if len(query.get("level", [])) != 1 or not query["level"][0].isdigit():
        return None

    return {
        **required,
        "level": query["level"][0],
        "backgroundView": "city",
        "currentCityId": str(city_id),
        "actionRequest": actionRequest,
        "ajax": "1",
    }


def tryFreeBuildingSpeedup(session, city_id, building):
    popup_params = {
        "view": "buildingSpeedup",
        "cityId": city_id,
        "position": building["position"],
        "backgroundView": "city",
        "currentCityId": city_id,
        "actionRequest": actionRequest,
        "ajax": "1",
    }
    response = session.post(params=popup_params)
    speedup_params = _getFreeSpeedupParams(response, city_id, building["position"])
    if speedup_params is None:
        return False

    wait(random.randint(1, SPEEDUP_RANDOMWAIT))
    with activity(session):
        response = session.post(params=popup_params)
        speedup_params = _getFreeSpeedupParams(response, city_id, building["position"])
        if speedup_params is None:
            return False

        session.post(params=speedup_params)
        return True


def waitForConstruction(session, city_id, final_lvl, construction_queue=None):
    observed_construction = False
    while True:
        html = session.get(city_url + city_id)
        city = getCity(html)

        construction_buildings = [
            building for building in city["position"] if "completed" in building
        ]
        if len(construction_buildings) == 0:
            if observed_construction:
                post_construction_delay = random.randint(60, 300)
                session.setStatus(f"Construction finished. Waiting {post_construction_delay} seconds before continuing.")
                wait(post_construction_delay)
            break

        observed_construction = True
        construction_building = construction_buildings[0]
        construction_time = construction_building["completed"]

        current_time = int(time.time())
        final_time = int(construction_time)
        seconds_to_wait = final_time - current_time
        current_remaining = max(0, seconds_to_wait)
        total_remaining = current_remaining + sum(construction_queue or [])
        status = "{}: {} -> level {} , current {} , total {}".format(
            city["cityName"],
            construction_building["name"],
            construction_building["level"] + 1,
            _formatRemainingTime(seconds_to_wait),
            _formatRemainingTime(total_remaining),
        )
        session.setStatus(status)

        if CONSTRUCTION_FREE_SPEEDUP and 0 < seconds_to_wait <= 300:
            if tryFreeBuildingSpeedup(session, city_id, construction_building):
                session.setStatus(status + " , free speedup sent")
                msg = "{}: Finished {} for 0 Ambrosia".format(
                    city["cityName"], construction_building["name"]
                )
                sendToBotDebug(session, msg, debugON_constructionList)
            else:
                session.setStatus(status + " , waiting for zero-cost speedup")
            wait(5)
            continue

        msg = "{}: I wait {:d} seconds so that {} gets to the level {:d}".format(
            city["cityName"],
            seconds_to_wait,
            construction_building["name"],
            construction_building["level"] + 1,
        )
        sendToBotDebug(session, msg, debugON_constructionList)
        if CONSTRUCTION_FREE_SPEEDUP:
            wait(max(5, seconds_to_wait - 300))
        else:
            wait(max(5, seconds_to_wait))

    html = session.get(city_url + city_id)
    city = getCity(html)
    return city


def expandBuilding(session, cityId, building, waitForResources, construction_queue=None):
    current_level = building["level"]
    if building["isBusy"]:
        current_level += 1
    levels_to_upgrade = building["upgradeTo"] - current_level
    position = building["position"]
    upgradeTo = building["upgradeTo"]
    time.sleep(random.randint(5, 15))

    for lv in range(levels_to_upgrade):
        city = waitForConstruction(session, cityId, upgradeTo, construction_queue)
        building = city["position"][position]

        if building["canUpgrade"] is False and waitForResources is True:
            while building["canUpgrade"] is False:
                time.sleep(60)
                seconds = getMinimumWaitingTime(session)
                html = session.get(city_url + cityId)
                city = getCity(html)
                building = city["position"][position]
                if seconds == 0:
                    break
                wait(seconds + 5)

        if building["canUpgrade"] is False:
            msg = "City:{}\n".format(city["cityName"])
            msg += "Building:{}\n".format(building["name"])
            msg += "The building could not be completed due to lack of resources.\n"
            msg += "Missed {:d} levels".format(levels_to_upgrade - lv)
            sendToBot(session, msg)
            return

        city = _waitForWineReserve(session, city, building, waitForResources)
        if city is None:
            return
        expected_level = city["position"][position]["level"]
        if expected_level >= upgradeTo:
            sendToBot(session, f"{city['cityName']}: target level already reached; no additional upgrade sent.")
            return

        with activity(session):
            city = getCity(session.get(city_url + str(cityId)))
            building = city["position"][position]
            if building["isBusy"] or building["level"] != expected_level:
                sendToBot(session, f"{city['cityName']}: construction changed while waiting; please requeue the upgrade.")
                return

            city = _waitForWineReserve(session, city, building, False)
            if city is None:
                return

            building = city["position"][position]
            if not building["canUpgrade"]:
                sendToBot(session, f"{city['cityName']}: construction stopped; resources changed before upgrade.")
                return

            url = "action=UpgradeExistingBuilding&actionRequest={}&cityId={}&position={:d}&level={}&activeTab=tabSendTransporter&backgroundView=city&currentCityId={}&templateView={}&ajax=1".format(
                actionRequest,
                cityId,
                position,
                building["level"],
                cityId,
                building["building"],
            )
            resp = session.post(url)
            html = session.get(city_url + cityId)
            city = getCity(html)
            building = city["position"][position]

        if building["isBusy"] is False:
            msg = "{}: The building {} was not extended".format(
                city["cityName"], building["name"]
            )
            sendToBot(session, msg)
            sendToBot(session, resp)
            return
        if construction_queue:
            construction_queue.pop(0)

        msg = "{}: The building {} is being extended to level {:d}.".format(
            city["cityName"], building["name"], building["level"] + 1
        )
        sendToBotDebug(session, msg, debugON_constructionList)

    city = waitForConstruction(session, cityId, upgradeTo, construction_queue)
    building = city["position"][position]
    msg = "{}: The building {} finished extending to level: {:d}.".format(
        city["cityName"], building["name"], building["level"]
    )
    sendToBotDebug(session, msg, debugON_constructionList)


def getCostsReducers(city):
    reducers_per_material = [0] * len(materials_names)
    assert len(reducers_per_material) == 5

    for building in city["position"]:
        if building["name"] == "empty":
            continue
        lv = building["level"]
        if building["building"] == "carpentering":
            reducers_per_material[0] = lv
        elif building["building"] == "vineyard":
            reducers_per_material[1] = lv
        elif building["building"] == "architect":
            reducers_per_material[2] = lv
        elif building["building"] == "optician":
            reducers_per_material[3] = lv
        elif building["building"] == "fireworker":
            reducers_per_material[4] = lv
    return reducers_per_material


def getResourcesNeeded(session, city, building, current_level, final_level, simulated_reducers=None):
    building_detail_url = "view=buildingDetail&buildingId=0&helpId=1&backgroundView=city&currentCityId={}&templateView=ikipedia&actionRequest={}&ajax=1".format(
        city["id"], actionRequest
    )
    building_detail_response = session.post(building_detail_url)
    building_detail = json.loads(building_detail_response, strict=False)
    building_html = building_detail[1][1][1]

    regex_building_detail = (
        r'<div class="(?:selected)? button_building '
        + re.escape(building["building"])
        + r'"\s*onmouseover="\$\(this\)\.addClass\(\'hover\'\);" onmouseout="\$\(this\)\.removeClass\(\'hover\'\);"\s*onclick="ajaxHandlerCall\(\'\?(.*?)\'\);'
    )
    match = re.search(regex_building_detail, building_html)
    building_costs_url = match.group(1)
    building_costs_url += "backgroundView=city&currentCityId={}&templateView=buildingDetail&actionRequest={}&ajax=1".format(
        city["id"], actionRequest
    )
    building_costs_response = session.post(building_costs_url)
    building_costs = json.loads(building_costs_response, strict=False)
    html_costs = building_costs[1][1][1]

    sessionData = session.getSessionData()
    if "reduccion_inv_max" in sessionData:
        costs_reduction = 14
    else:
        url = "view=noViewChange&researchType=economy&backgroundView=city&currentCityId={}&templateView=researchAdvisor&actionRequest={}&ajax=1".format(
            city["id"], actionRequest
        )
        rta = session.post(url)
        rta = json.loads(rta, strict=False)
        studies = rta[2][1]["new_js_params"]
        studies = json.loads(studies, strict=False)
        studies = studies["currResearchType"]

        costs_reduction = 0
        for study in studies:
            if studies[study]["liClass"] != "explored":
                continue
            link = studies[study]["aHref"]
            if "2020" in link:
                costs_reduction += 2
            elif "2060" in link:
                costs_reduction += 4
            elif "2100" in link:
                costs_reduction += 8

        if costs_reduction == 14:
            sessionData["reduccion_inv_max"] = True
            session.setSessionData(sessionData)

    costs_reduction /= 100
    costs_reduction = 1 - costs_reduction

    if simulated_reducers is None:
        costs_reductions = getCostsReducers(city)
    else:
        costs_reductions = simulated_reducers

    resources_types = re.findall(
        r'<th class="costs"><img src="(.*?)\.png"/></th>', html_costs
    )[:-1]

    matches = re.findall(
        r'<td class="level">\d+</td>(?:\s+<td class="costs">.*?</td>)+', html_costs
    )

    final_costs = [0] * len(materials_names)
    construction_time = 0
    construction_times = []
    levels_to_upgrade = 0
    for match in matches:
        lv = re.search(r'"level">(\d+)</td>', match).group(1)
        lv = int(lv)

        if lv <= current_level:
            continue
        if lv > final_level:
            break

        levels_to_upgrade += 1
        level_time = _parseConstructionTime(match)
        if level_time is not None:
            construction_time += level_time
            construction_times.append(level_time)

        costs = re.findall(r'<td class="costs"><div.*>([\d,\.\s\xa0]*)</div></div></td>', match)
        costs = [value.replace('\xa0', '').replace(' ', '') for value in costs]

        for i in range(len(costs)):
            resource_type = checkhash("https:" + resources_types[i] + ".png")

            for j in range(len(materials_names_tec)):
                name = materials_names_tec[j]
                if resource_type == name:
                    resource_index = j
                    break

            cost = costs[i]
            cost = cost.replace(",", "").replace(".", "")
            cost = 0 if cost == "" else int(cost)

            investigation_multiplier = Decimal(str(costs_reduction))
            building_reduction = Decimal(costs_reductions[resource_index]) / Decimal(100)
            final_multiplier = investigation_multiplier - building_reduction
            real_cost = Decimal(cost) * (final_multiplier / investigation_multiplier)
            final_costs[resource_index] += math.ceil(real_cost)

        if building["building"] == "carpentering":
            costs_reductions[0] = min(50, costs_reductions[0] + 1)
        elif building["building"] == "vineyard":
            costs_reductions[1] = min(50, costs_reductions[1] + 1)
        elif building["building"] == "architect":
            costs_reductions[2] = min(50, costs_reductions[2] + 1)
        elif building["building"] == "optician":
            costs_reductions[3] = min(50, costs_reductions[3] + 1)
        elif building["building"] == "fireworker":
            costs_reductions[4] = min(50, costs_reductions[4] + 1)

    if levels_to_upgrade < final_level - current_level:
        print(
            "This building only allows you to expand {:d} more levels".format(
                levels_to_upgrade
            )
        )
        msg = "Expand {:d} levels? [Y/n]:".format(levels_to_upgrade)
        rta = read(msg=msg, values=["Y", "y", "N", "n", ""])
        if rta.lower() == "n":
            return [-2, -2, -2, -2, -2]

    building["constructionTime"] = construction_time
    building["constructionTimes"] = construction_times
    return final_costs


def _round_up_resources(amount):
    if amount < 1000:
        return math.ceil(amount / 100) * 100
    elif amount < 10000:
        return math.ceil(amount / 500) * 500
    else:
        return math.ceil(amount / 1000) * 1000


def sendResourcesNeeded(session, destination_city_id, city_origins, missing_resources, useFreighters=False, useRounding=False):
    info = "\nTransport resources to upload building\n"

    try:
        html = session.get(city_url + destination_city_id)
        cityD = getCity(html)

        combined = {}
        refreshed_origins = {}
        room = list(cityD.get("freeSpaceForResources", [max(0, cityD.get("storageCapacity", 0) - amount) for amount in cityD["availableResources"]]))

        for i in range(len(materials_names)):
            missing = missing_resources[i]
            if missing <= 0:
                continue

            target = _round_up_resources(missing) if useRounding else missing
            if target > room[i]:
                sendToBot(session, f"{cityD['cityName']}: insufficient free storage for {materials_names[i]} shipment ({target:,} requested, {room[i]:,} free). Shipment capped.")
                target = max(0, room[i])
            remaining = target

            for cityOrigin in city_origins[i]:
                if remaining == 0:
                    break

                origin_id = cityOrigin["id"]
                if origin_id not in refreshed_origins:
                    refreshed_origins[origin_id] = getCity(session.get(city_url + str(origin_id)))
                cityOrigin = refreshed_origins[origin_id]
                available = cityOrigin["availableResources"][i]
                
                # Protectia se aplica strict pentru vin!
                if i == WINE_INDEX:
                    available = max(0, available - getWineReserve(session, cityOrigin))

                send = min(available, remaining)
                if send <= 0:
                    continue
                remaining -= send

                city_id = cityOrigin["id"]
                if city_id not in combined:
                    combined[city_id] = (cityOrigin, [0] * len(materials_names))
                combined[city_id][1][i] += send
                cityOrigin["availableResources"][i] -= send

            if remaining > 0:
                if i == WINE_INDEX:
                    sendToBot(session, f"{cityD['cityName']}: {remaining:,} wine could not be supplied without dipping into protected wine reserve.")
                else:
                    sendToBot(session, f"{cityD['cityName']}: {remaining:,} {materials_names[i]} could not be supplied (insufficient available stock in origin cities).")

        routes = []
        for city_id, (cityOrigin, toSend) in combined.items():
            route = (cityOrigin, cityD, cityD["islandId"], *toSend)
            routes.append(route)

        if routes:
            executeRoutes(session, routes, useFreighters)
    except Exception as e:
        msg = "Error in:\n{}\nCause:\n{}".format(info, traceback.format_exc())
        sendToBot(session, msg)


def chooseResourceProviders(session, cities_ids, cities, city_id, resource, missing):
    send_resources_flag = True
    expand_flag = True

    banner()
    print("From what cities obtain {}?".format(materials_names[resource].lower()))

    tradegood_initials = [material_name[0] for material_name in materials_names]
    maxName = max(
        [len(cities[city]["name"]) for city in cities if cities[city]["id"] != city_id]
    )

    origin_cities = []
    total_available = 0
    for cityId in cities_ids:
        if cityId == city_id:
            continue

        html = session.get(city_url + cityId)
        city = getCity(html)

        available = city["availableResources"][resource]
        # Protectie aplicata doar la vin
        if resource == WINE_INDEX:
            available = max(0, available - getWineReserve(session, city))
        if available == 0:
            continue

        tradegood_initial = tradegood_initials[int(cities[cityId]["tradegood"])]
        pad = " " * (maxName - len(cities[cityId]["name"]))
        is_producer = (int(cities[cityId]["tradegood"]) == int(resource))
        msg = "{}{} ({}): {} [{}]:".format(
            pad,
            cities[cityId]["name"],
            tradegood_initial,
            addThousandSeparator(available),
            ("y/N", "Y/n")[is_producer == True]
        )
        choice = read(msg=msg, values=["Y", "y", "N", "n", ""], default=("N", "Y")[is_producer == True])
        if choice.lower() == "n":
            continue

        total_available += available
        origin_cities.append(city)
        if total_available >= missing:
            return origin_cities, send_resources_flag, expand_flag

    print("\nThere are not enough resources.")

    if len(origin_cities) > 0:
        print("\nSend the resources anyway? [Y/n]")
        choice = read(values=["y", "Y", "n", "N", ""])
        if choice.lower() == "n":
            send_resources_flag = False

    print("\nTry to expand the building anyway? [y/N]")
    choice = read(values=["y", "Y", "n", "N", ""])
    if choice.lower() == "n" or choice == "":
        expand_flag = False

    return origin_cities, send_resources_flag, expand_flag


def sendResourcesMenu(session, city_id, missing):
    cities_ids, cities = getIdsOfCities(session)
    origins = {}
    
    send_resources_flag = True
    expand_flag = True
    
    for resource in range(len(missing)):
        if missing[resource] <= 0:
            continue

        origin_cities, current_send_resources, current_expand = chooseResourceProviders(
            session, cities_ids, cities, city_id, resource, missing[resource]
        )
        send_resources_flag = current_send_resources
        expand_flag = current_expand
        if send_resources_flag is False and expand_flag:
            print("\nThe building will be expanded if possible.")
            enter()
            return None, expand_flag
        elif send_resources_flag is False:
            return None, expand_flag
        origins[resource] = origin_cities

    if expand_flag:
        print("\nThe resources will be sent and the building will be expanded if possible.")
    else:
        print("\nThe resources will be sent.")

    enter()

    return origins, expand_flag


def getBuildingsToExpand(session, cityId):
    html = session.get(city_url + cityId)
    city = getCity(html)

    banner()
    print("Which buildings do you want to expand? Separate numbers with commas (7, 1, 3, 5, ...)\n")
    print("(0)\t\texit")
    buildings = [
        building for building in city["position"] if building["name"] != "empty"
    ]
    for i in range(len(buildings)):
        building = buildings[i]
        level = building["level"]

        if building["isMaxLevel"] is True:
            color = bcolors.BLACK
        elif building["canUpgrade"] is True:
            color = bcolors.GREEN
        else:
            color = bcolors.RED
        if level < 10:
            level = " " + str(level)
        else:
            level = str(level)
        if building["isBusy"]:
            level = level + "+"
        print("({:d})\tlv:{}\t{}{}{}".format(i + 1, level, color, building["name"], bcolors.ENDC))

    selected_building_ids = read().split(",")
    selected_building_ids = [int(id.strip()) for id in selected_building_ids if id.strip().isdigit()]

    if len(selected_building_ids) == 0 or 0 in selected_building_ids:
        return None

    selected_buildings = []
    for building_id in selected_building_ids:
        building = buildings[building_id - 1]
        current_level = int(building["level"])
        if building["isBusy"]:
            current_level += 1

        banner()
        print("building:{}".format(building["name"]))
        print("current level:{}".format(current_level))

        final_level = read(min=current_level, msg="increase to level:")
        building["upgradeTo"] = final_level
        selected_buildings.append(building)

    return selected_buildings


@cache
def checkhash(url):
    m = hashlib.md5()
    r = requests.get(url)
    for data in r.iter_content(8192):
        m.update(data)
        if m.hexdigest() == config.material_img_hash[0]:
            material = "wood"
        elif m.hexdigest() == config.material_img_hash[1]:
            material = "wine"
        elif m.hexdigest() == config.material_img_hash[2]:
            material = "marble"
        elif m.hexdigest() == config.material_img_hash[3]:
            material = "glass"
        elif m.hexdigest() == config.material_img_hash[4]:
            material = "sulfur"
        else:
            continue
    return material


@task("constructionList")
def do_it(session, city_id, city_name, buildings, shipments, expand_overall, wait_resources,
          construction_queue, total_time):
    session.setStatus(
        "{}: queued {} upgrades | total {}".format(
            city_name, len(construction_queue), _formatRemainingTime(total_time)
        )
    )
    info = "\nUpgrade building\n"
    building_log_name = buildings[0]["name"] if len(buildings) == 1 else "Multiple buildings"
    info = info + "City: {}\nBuilding: {}".format(city_name, building_log_name)
    setInfoSignal(session, info)
    
    threads = []
    for shipment in shipments:
        t = threading.Thread(
            target=sendResourcesNeeded,
            args=(
                session,
                city_id,
                shipment["origins"],
                shipment["missing"],
                shipment["useFreighters"],
                shipment["useRounding"],
            ),
        )
        t.start()
        threads.append(t)

    try:
        if expand_overall:
            for building in buildings:
                expandBuilding(
                    session, city_id, building, wait_resources, construction_queue
                )
        else:
            for t in threads:
                t.join()
    except Exception as e:
        msg = "Error in:\n{}\nCause:\n{}".format(info, traceback.format_exc())
        sendToBot(session, msg)


@configurator
def constructionList(session, event, stdin_fd, predetermined_input):
    banner()
    wait_resources = False
    print("In which city do you want to expand buildings?")
    city = chooseCity(session)
    cityId = city["id"]
    buildings = getBuildingsToExpand(session, cityId)
    if buildings is None or len(buildings) == 0:
        return None

    simulated_resources = list(city["availableResources"])
    simulated_reducers = getCostsReducers(city)

    for b in city["position"]:
        if b["name"] != "empty" and b.get("isBusy", False):
            if b["building"] == "carpentering":
                simulated_reducers[0] = min(50, simulated_reducers[0] + 1)
            elif b["building"] == "vineyard":
                simulated_reducers[1] = min(50, simulated_reducers[1] + 1)
            elif b["building"] == "architect":
                simulated_reducers[2] = min(50, simulated_reducers[2] + 1)
            elif b["building"] == "optician":
                simulated_reducers[3] = min(50, simulated_reducers[3] + 1)
            elif b["building"] == "fireworker":
                simulated_reducers[4] = min(50, simulated_reducers[4] + 1)

    confirmed_buildings = []
    shipments = []
    expand_overall = True

    for building in buildings:
        current_level = building["level"]
        if building["isBusy"]:
            current_level += 1
        final_level = building["upgradeTo"]

        current_reducers = list(simulated_reducers)

        resourcesNeeded = getResourcesNeeded(
            session, city, building, current_level, final_level, current_reducers
        )

        if -2 in resourcesNeeded:
            print(f"\nSkipping {building['name']}.\n")
            continue

        if -1 in resourcesNeeded:
            return None

        waiting_seconds = max(0, city.get("endUpgradeTime", 0) - time.time())
        waiting_seconds += sum(b.get("constructionTime", 0) for b in confirmed_buildings)
        waiting_seconds += WINE_TRANSPORT_BUFFER_HOURS * 3600
        
        # Doar vinul primeste rezerva
        required = _constructionRequirements(session, city, resourcesNeeded, waiting_seconds)
        over_capacity = getResourcesOverCapacity(city, required)
        if over_capacity:
            print("[WARNING] Insufficient storage for the planned resources, including wine reserve:")
            for name, needed, capacity in over_capacity:
                print(f"  - {name}: needed {needed:,}, capacity {capacity:,}. Upgrade storage or shorten the construction list.")
        
        if resourcesNeeded[WINE_INDEX] > 0:
            print(f"Wine protected (reserve plus waiting margin): {required[WINE_INDEX] - resourcesNeeded[WINE_INDEX]:,}")
            
        missing = [0] * len(materials_names)
        for i in range(len(materials_names)):
            if simulated_resources[i] < required[i]:
                missing[i] = required[i] - simulated_resources[i]

        if sum(missing) > 0:
            print("\nMaterials needed for {} (lv {:d} -> {:d}):".format(building["name"], current_level, final_level))
            for i, name in enumerate(materials_names):
                amount = resourcesNeeded[i]
                if amount == 0:
                    continue
                print("- {}: {}".format(name, addThousandSeparator(max(0, amount))))
            print("")

            print("Missing:")
            for i in range(len(materials_names)):
                if missing[i] <= 0:
                    continue
                name = materials_names[i].lower()
                print("{} of {}".format(addThousandSeparator(missing[i]), name))
            print("")

            print("Automatically transport resources? [Y/n]")
            rta = read(values=["y", "Y", "n", "N", ""])
            if rta.lower() == "n":
                print("Proceed anyway? [Y/n]")
                rta = read(values=["y", "Y", "n", "N", ""])
                if rta.lower() == "n":
                    print(f"\nSkipping {building['name']} due to user cancellation.\n")
                    continue
            else:
                print("What type of ships do you want to use? (Default: Trade ships)")
                print("(1) Trade ships")
                print("(2) Freighters")
                shiptype = read(min=1, max=2, digit=True, empty=True)
                if shiptype == '':
                    shiptype = 1
                if shiptype == 1:
                    useFreighters = False
                elif shiptype == 2:
                    useFreighters = True
                print("Round up transport amounts? [Y/n]")
                print("(amounts will be rounded up but capped at what each city can provide)")
                rta_round = read(values=["y", "Y", "n", "N", ""])
                useRounding = rta_round.lower() != "n"
                wait_resources = True
                origins, current_expand = sendResourcesMenu(session, cityId, missing)
                
                if not current_expand:
                    expand_overall = False
                
                if origins:
                    shipments.append({
                        "origins": origins,
                        "missing": missing,
                        "useFreighters": useFreighters,
                        "useRounding": useRounding
                    })
        else:
            print("\nMaterials needed for {} (lv {:d} -> {:d}):".format(building["name"], current_level, final_level))
            for i, name in enumerate(materials_names):
                amount = resourcesNeeded[i]
                if amount == 0:
                    continue
                print("- {}: {}".format(name, addThousandSeparator(max(0, amount))))
            print("")

            print("You have enough materials")
            print("Proceed? [Y/n]")
            rta = read(values=["y", "Y", "n", "N", ""])
            if rta.lower() == "n":
                print(f"\nSkipping {building['name']} due to user cancellation.\n")
                continue

        confirmed_buildings.append(building)
        simulated_reducers = current_reducers

        for i in range(len(materials_names)):
            if simulated_resources[i] < resourcesNeeded[i]:
                simulated_resources[i] = 0
            else:
                simulated_resources[i] -= resourcesNeeded[i]

    buildings = confirmed_buildings

    total_time = sum(building["constructionTime"] for building in buildings)
    active_time = int(city.get("endUpgradeTime", 0)) - int(time.time())
    total_time += max(0, active_time)
    construction_queue = [
        duration
        for building in buildings
        for duration in building["constructionTimes"]
    ]
    if buildings:
        print("\nTotal construction time: {}".format(daysHoursMinutes(total_time)))

    if (not buildings or len(buildings) == 0) and len(shipments) == 0:
        return None

    return {
        "session": session,
        "city_id": cityId,
        "city_name": city["cityName"],
        "buildings": buildings,
        "shipments": shipments,
        "expand_overall": expand_overall,
        "wait_resources": wait_resources,
        "construction_queue": construction_queue,
        "total_time": total_time,
    }
