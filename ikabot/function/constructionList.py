#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import hashlib
import json
import math
import random
import re
import threading
import time
import traceback
from decimal import *
from functools import cache

import requests

from ikabot.config import *
from ikabot.helpers.botComm import *
from ikabot.helpers.getJson import getCity
from ikabot.helpers.gui import *
from ikabot.helpers.pedirInfo import *
from ikabot.helpers.planRoutes import *
from ikabot.helpers.process import set_child_mode
from ikabot.helpers.resources import getAvailableResources
from ikabot.helpers.signals import setInfoSignal
from ikabot.helpers.varios import *
from ikabot.web.session import normal_get

# Safe import for cross-account lock (falls back cleanly if not present)
try:
    from ikabot.helpers.crossAccountLock import acquire_activity_lock, release_activity_lock
except ImportError:
    def acquire_activity_lock(username=None):
        return True

    def release_activity_lock():
        pass

getcontext().prec = 30

sendResources = True
expand = True
thread = None

WINE_INDEX = 1  # In materials_names, index 1 is always "Wine"
WINE_RESERVE = 10000  # Amount of wine that must remain in the city after the upgrade
WAREHOUSE_CAPACITY_KEY = "storageCapacity"


def waitForConstruction(session, city_id, final_lvl):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    city_id : int
    final_lvl : int

    Returns
    -------
    city : dict
    """
    while True:
        html = session.get(city_url + city_id)
        city = getCity(html)

        construction_buildings = [
            building for building in city["position"] if "completed" in building
        ]
        if len(construction_buildings) == 0:
            break

        construction_building = construction_buildings[0]
        construction_time = construction_building["completed"]

        current_time = int(time.time())
        final_time = int(construction_time)
        seconds_to_wait = final_time - current_time

        # Randomized human-like pause (1-5 min) before requesting the next level
        post_construction_delay = random.randint(60, 300)

        msg = "{}: I wait {:d} seconds so that {} gets to the level {:d}".format(
            city["cityName"],
            seconds_to_wait,
            construction_building["name"],
            construction_building["level"] + 1,
        )
        sendToBotDebug(session, msg, debugON_constructionList)
        session.setStatus(
            f"Waiting until {getDateTime(time.time() + seconds_to_wait + post_construction_delay)[8:]}, "
            f"{construction_building['name']} {construction_building['level']} -> "
            f"{construction_building['level'] + 1} in {city['name']}, final lvl: {final_lvl}"
        )
        wait(seconds_to_wait + post_construction_delay)

    html = session.get(city_url + city_id)
    city = getCity(html)
    return city


def expandBuilding(session, cityId, building, waitForResources):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    cityId : int
    building : dict
    waitForResources : bool
    """
    current_level = building["level"]
    if building["isBusy"]:
        current_level += 1
    levels_to_upgrade = building["upgradeTo"] - current_level
    position = building["position"]
    upgradeTo = building["upgradeTo"]
    time.sleep(random.randint(5, 15))  # Avoid race conditions with sendResourcesNeeded

    for lv in range(levels_to_upgrade):
        city = waitForConstruction(session, cityId, upgradeTo)
        building = city["position"][position]

        if building["canUpgrade"] is False and waitForResources is True:
            while building["canUpgrade"] is False:
                time.sleep(60)
                seconds = getMinimumWaitingTime(session)
                html = session.get(city_url + cityId)
                city = getCity(html)
                building = city["position"][position]
                # If no ships are coming, exit regardless
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

        url = (
            "action=UpgradeExistingBuilding&actionRequest={}&cityId={}&position={:d}&level={}&"
            "activeTab=tabSendTransporter&backgroundView=city&currentCityId={}&templateView={}&ajax=1"
        ).format(
            actionRequest,
            cityId,
            position,
            building["level"],
            cityId,
            building["building"],
        )

        # Cross-account lock only around the brief upgrade request
        lock_acquired = acquire_activity_lock(session.username)
        if not lock_acquired:
            session.setStatus("Cross-account activity lock timeout; skipping upgrade")
            return
        try:
            resp = session.post(url)
            html = session.get(city_url + cityId)
            city = getCity(html)
            building = city["position"][position]
        finally:
            if lock_acquired:
                release_activity_lock()

        if building["isBusy"] is False:
            msg = "{}: The building {} was not extended".format(
                city["cityName"], building["name"]
            )
            sendToBot(session, msg)
            sendToBot(session, resp)
            return

        msg = "{}: The building {} is being extended to level {:d}.".format(
            city["cityName"], building["name"], building["level"] + 1
        )
        sendToBotDebug(session, msg, debugON_constructionList)

    msg = "{}: The building {} finished extending to level: {:d}.".format(
        city["cityName"], building["name"], building["level"] + 1
    )
    sendToBotDebug(session, msg, debugON_constructionList)


def getCostsReducers(city):
    """
    Parameters
    ----------
    city : dict

    Returns
    -------
    reducers_per_material_level : list[int]
    """
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
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    city : dict
    building : dict
    current_level : int
    final_level : int
    simulated_reducers : list[int]

    Returns
    -------
    costs_per_material : list[int]
    """
    building_detail_url = (
        "view=buildingDetail&buildingId=0&helpId=1&backgroundView=city&currentCityId={}&"
        "templateView=ikipedia&actionRequest={}&ajax=1"
    ).format(city["id"], actionRequest)
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
        url = (
            "view=noViewChange&researchType=economy&backgroundView=city&currentCityId={}&"
            "templateView=researchAdvisor&actionRequest={}&ajax=1"
        ).format(city["id"], actionRequest)
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
        costs_reductions = list(simulated_reducers)

    resources_types = re.findall(
        r'<th class="costs"><img src="(.*?)\.png"/></th>', html_costs
    )[:-1]

    matches = re.findall(
        r'<td class="level">\d+</td>(?:\s+<td class="costs">.*?</td>)+', html_costs
    )

    final_costs = [0] * len(materials_names)
    levels_to_upgrade = 0
    for match in matches:
        lv = int(re.search(r'"level">(\d+)</td>', match).group(1))

        if lv <= current_level:
            continue
        if lv > final_level:
            break

        levels_to_upgrade += 1
        costs = re.findall(r'<td class="costs"><div.*>([\d,\.\s\xa0]*)</div></div></td>', match)
        costs = [value.replace('\xa0', '').replace(' ', '') for value in costs]

        for i in range(len(costs)):
            resource_type = checkhash("https:" + resources_types[i] + ".png")

            resource_index = None
            for j in range(len(materials_names_tec)):
                if resource_type == materials_names_tec[j]:
                    resource_index = j
                    break

            if resource_index is None:
                continue

            cost = costs[i].replace(",", "").replace(".", "")
            cost = 0 if cost == "" else int(cost)

            investigation_multiplier = Decimal(str(costs_reduction))
            building_reduction = Decimal(costs_reductions[resource_index]) / Decimal(100)
            final_multiplier = investigation_multiplier - building_reduction

            # Direct proportional calculation without guessing base cost
            real_cost = Decimal(cost) * (final_multiplier / investigation_multiplier)
            final_costs[resource_index] += math.ceil(real_cost)

        # Update progressive discounts if a reducer building is being upgraded
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
        print(f"This building only allows you to expand {levels_to_upgrade:d} more levels")
        msg = f"Expand {levels_to_upgrade:d} levels? [Y/n]:"
        rta = read(msg=msg, values=["Y", "y", "N", "n", ""])
        if rta.lower() == "n":
            return [-2, -2, -2, -2, -2]

    return final_costs


def getWarehouseCapacity(city):
    """
    Parameters
    ----------
    city : dict

    Returns
    -------
    capacity : int or None
    """
    return city.get(WAREHOUSE_CAPACITY_KEY)


def getResourcesOverCapacity(city, resourcesNeeded):
    """
    Parameters
    ----------
    city : dict
    resourcesNeeded : list[int]

    Returns
    -------
    over_capacity : list of tuple (str, int, int)
    """
    capacity = getWarehouseCapacity(city)
    if capacity is None:
        return []

    over_capacity = []
    for i, needed in enumerate(resourcesNeeded):
        if needed > capacity:
            over_capacity.append((materials_names[i], needed, capacity))

    return over_capacity


def _round_up_resources(amount):
    if amount < 1000:
        return math.ceil(amount / 100) * 100
    elif amount < 10000:
        return math.ceil(amount / 500) * 500
    else:
        return math.ceil(amount / 1000) * 1000


def sendResourcesNeeded(session, destination_city_id, city_origins, missing_resources, useFreighters=False, useRounding=False):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    destination_city_id : int
    city_origins : dict
    missing_resources : list[int]
    useFreighters : bool
    useRounding : bool
    """
    info = "\nTransport resources to upload building\n"
    try:
        html = session.get(city_url + destination_city_id)
        cityD = getCity(html)

        combined = {}  # origin_city_id -> (cityOrigin, toSend_list)

        for i in range(len(materials_names)):
            missing = missing_resources[i]
            if missing <= 0:
                continue

            target = _round_up_resources(missing) if useRounding else missing
            remaining = target

            for cityOrigin in city_origins[i]:
                if remaining == 0:
                    break

                available = cityOrigin["availableResources"][i]
                send = min(available, remaining)
                remaining -= send

                city_id = cityOrigin["id"]
                if city_id not in combined:
                    combined[city_id] = (cityOrigin, [0] * len(materials_names))
                combined[city_id][1][i] += send

        routes = []
        for city_id, (cityOrigin, toSend) in combined.items():
            route = (cityOrigin, cityD, cityD["islandId"], *toSend)
            routes.append(route)

        executeRoutes(session, routes, useFreighters)
    except Exception as e:
        msg = f"Error in:\n{info}\nCause:\n{traceback.format_exc()}"
        sendToBot(session, msg)


def chooseResourceProviders(session, cities_ids, cities, city_id, resource, missing):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    cities_ids : list[int]
    cities : dict[int, dict]
    city_id : int
    resource : int
    missing : int
    """
    global sendResources, expand
    sendResources = True
    expand = True

    banner()
    print(f"From what cities obtain {materials_names[resource].lower()}?")

    tradegood_initials = [material_name[0] for material_name in materials_names]
    maxName = max([len(cities[c]["name"]) for c in cities if cities[c]["id"] != city_id])

    origin_cities = []
    total_available = 0
    for cityId in cities_ids:
        if cityId == city_id:
            continue

        html = session.get(city_url + cityId)
        city = getCity(html)

        available = city["availableResources"][resource]
        if available == 0:
            continue

        tradegood_initial = tradegood_initials[int(cities[cityId]["tradegood"])]
        pad = " " * (maxName - len(cities[cityId]["name"]))
        is_producer = int(cities[cityId]["tradegood"]) == int(resource)
        msg = "{}{} ({}): {} [{}]:".format(
            pad,
            cities[cityId]["name"],
            tradegood_initial,
            addThousandSeparator(available),
            "Y/n" if is_producer else "y/N"
        )
        choice = read(msg=msg, values=["Y", "y", "N", "n", ""], default="Y" if is_producer else "N")
        if choice.lower() == "n":
            continue

        total_available += available
        origin_cities.append(city)
        if total_available >= missing:
            return origin_cities

    print("\nThere are not enough resources.")

    if len(origin_cities) > 0:
        print("\nSend the resources anyway? [Y/n]")
        choice = read(values=["y", "Y", "n", "N", ""])
        if choice.lower() == "n":
            sendResources = False

    print("\nTry to expand the building anyway? [y/N]")
    choice = read(values=["y", "Y", "n", "N", ""])
    if choice.lower() == "n" or choice == "":
        expand = False

    return origin_cities


def sendResourcesMenu(session, city_id, missing, useFreighters=False, useRounding=False):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    city_id : int
    missing : list[int]
    useFreighters : bool
    useRounding : bool
    """
    global thread
    cities_ids, cities = getIdsOfCities(session)
    origins = {}

    for resource in range(len(missing)):
        if missing[resource] <= 0:
            continue

        origin_cities = chooseResourceProviders(
            session, cities_ids, cities, city_id, resource, missing[resource]
        )
        if sendResources is False and expand:
            print("\nThe building will be expanded if possible.")
            enter()
            return
        elif sendResources is False:
            return
        origins[resource] = origin_cities

    if expand:
        print("\nThe resources will be sent and the building will be expanded if possible.")
    else:
        print("\nThe resources will be sent.")

    enter()

    thread = threading.Thread(
        target=sendResourcesNeeded,
        args=(
            session,
            city_id,
            origins,
            missing,
            useFreighters,
            useRounding,
        ),
    )
    thread.start()


def getBuildingsToExpand(session, cityId):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    cityId : int

    Returns
    -------
    buildings : list of dict
    """
    html = session.get(city_url + cityId)
    city = getCity(html)

    banner()
    print("Which buildings do you want to expand? Separate numbers with commas (7, 1, 3, 5, ...)\n")
    print("(0)\t\texit")
    buildings = [b for b in city["position"] if b["name"] != "empty"]

    for i, building in enumerate(buildings):
        level = building["level"]
        if building["isMaxLevel"] is True:
            color = bcolors.BLACK
        elif building["canUpgrade"] is True:
            color = bcolors.GREEN
        else:
            color = bcolors.RED

        level_str = f" {level}" if level < 10 else str(level)
        if building["isBusy"]:
            level_str += "+"
        print(f"({i + 1:d})\tlv:{level_str}\t{color}{building['name']}{bcolors.ENDC}")

    selected_building_ids = read().split(",")
    selected_building_ids = [int(i.strip()) for i in selected_building_ids if i.strip().isdigit()]

    if len(selected_building_ids) == 0 or 0 in selected_building_ids:
        return None

    selected_buildings = []
    for building_id in selected_building_ids:
        building = buildings[building_id - 1]
        current_level = int(building["level"])
        if building["isBusy"]:
            current_level += 1

        banner()
        print(f"building:{building['name']}")
        print(f"current level:{current_level}")

        final_level = read(min=current_level, msg="increase to level:")
        building["upgradeTo"] = final_level
        selected_buildings.append(building)

    return selected_buildings


@cache
def checkhash(url):
    m = hashlib.md5()
    material = None
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


def constructionList(session, event, stdin_fd, predetermined_input):
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
        global expand, sendResources
        expand = True
        sendResources = True

        banner()
        wait_resources = False
        print("In which city do you want to expand buildings?")
        city = chooseCity(session)
        cityId = city["id"]
        buildings = getBuildingsToExpand(session, cityId)
        if buildings is None or len(buildings) == 0:
            event.set()
            return

        # Simulated resources and reducers
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
                event.set()
                return

            # Check warehouse storage capacity
            over_capacity = getResourcesOverCapacity(city, resourcesNeeded)
            if over_capacity:
                print("\n[WARNING] The warehouse doesn't have enough capacity for:")
                for name, needed, cap in over_capacity:
                    print(
                        f"  - {name.lower()}: needed {addThousandSeparator(needed)}, "
                        f"warehouse capacity {addThousandSeparator(cap)} -> upgrade the Warehouse!"
                    )
                print("")

            # Calculate missing resources keeping the wine reserve intact
            missing = [0] * len(materials_names)
            for i in range(len(materials_names)):
                available = simulated_resources[i]
                if i == WINE_INDEX:
                    available = max(0, available - WINE_RESERVE)
                if available < resourcesNeeded[i]:
                    missing[i] = resourcesNeeded[i] - available

            if sum(missing) > 0:
                print(f"\nMaterials needed for {building['name']} (lv {current_level:d} -> {final_level:d}):")
                for i, name in enumerate(materials_names):
                    amount = resourcesNeeded[i]
                    if amount == 0:
                        continue
                    print(f"- {name}: {addThousandSeparator(max(0, amount))}")
                print("")

                print("Missing:")
                for i in range(len(materials_names)):
                    if missing[i] <= 0:
                        continue
                    print(f"{addThousandSeparator(missing[i])} of {materials_names[i].lower()}")
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
                    useFreighters = (shiptype == 2)

                    print("Round up transport amounts? [Y/n]")
                    print("(amounts will be rounded up but capped at what each city can provide)")
                    rta_round = read(values=["y", "Y", "n", "N", ""])
                    useRounding = rta_round.lower() != "n"
                    wait_resources = True
                    sendResourcesMenu(session, cityId, missing, useFreighters, useRounding)
            else:
                print(f"\nMaterials needed for {building['name']} (lv {current_level:d} -> {final_level:d}):")
                for i, name in enumerate(materials_names):
                    amount = resourcesNeeded[i]
                    if amount == 0:
                        continue
                    print(f"- {name}: {addThousandSeparator(max(0, amount))}")
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

    except KeyboardInterrupt:
        event.set()
        return

    if (not buildings or len(buildings) == 0) and not thread:
        event.set()
        return

    set_child_mode(session)
    event.set()

    building_log_name = buildings[0]["name"] if len(buildings) == 1 else "Multiple buildings"
    info = f"\nUpgrade building\nCity: {city['cityName']}\nBuilding: {building_log_name}"

    setInfoSignal(session, info)
    try:
        if expand:
            for building in buildings:
                expandBuilding(session, cityId, building, wait_resources)
        elif thread:
            thread.join()
    except Exception as e:
        msg = f"Error in:\n{info}\nCause:\n{traceback.format_exc()}"
        print(msg)
        sendToBot(session, msg)
    finally:
        session.logout()