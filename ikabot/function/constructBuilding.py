#! /usr/bin/env python3
# -*- coding: utf-8 -*-


import json
import re

from ikabot.helpers.decorators import configurator, task
from ikabot.helpers.botComm import sendToBot
from ikabot.helpers.signals import setInfoSignal
from ikabot.config import *
from ikabot.helpers.gui import *
from ikabot.helpers.pedirInfo import *
from ikabot.helpers.varios import addThousandSeparator
from ikabot.helpers.getJson import getCity
from ikabot.function.constructionList import (
    _constructionRequirements, getResourcesOverCapacity,
    sendResourcesMenu, sendResourcesNeeded,
)
from ikabot.helpers.planRoutes import getMinimumWaitingTime
from ikabot.helpers.varios import wait
from ikabot.helpers.activityCoordinator import activity


def supplyBuildingResources(session, city, costs):
    """Collect transport choices without sending or waiting in the menu."""
    required = _constructionRequirements(session, city, costs)
    over_capacity = getResourcesOverCapacity(city, required)
    if over_capacity:
        print("Insufficient storage for construction costs plus wine reserve:")
        for name, needed, capacity in over_capacity:
            print(f"- {name}: needed {needed:,}, capacity {capacity:,}. Upgrade storage first.")
        return None
    missing = [max(0, need - have) for need, have in zip(required, city["availableResources"])]
    if not any(missing):
        return {"origins": None, "missing": missing, "useFreighters": False}
    print("\nAutomatically transport missing resources? [Y/n]")
    if read(values=["y", "Y", "n", "N", ""]).lower() == "n":
        return None
    print("Ship type: (1) Trade ships (2) Freighters")
    ship_type = read(min=1, max=2, digit=True, default=1)
    origins, proceed = sendResourcesMenu(session, city["id"], missing)
    if not proceed or not origins:
        return None
    return {"origins": origins, "missing": missing, "useFreighters": ship_type == 2}



def parseBuildingCosts(html_block):
    costs = [0] * len(materials_names)
    resource_map = {
        "wood": 0,
        "wine": 1,
        "marble": 2,
        "crystal": 3,
        "glass": 3,
        "sulfur": 4,
    }
    matches = re.findall(
        r'<li\s+class="(\w+)[^"]*"[^>]*\s+title="[^"]*?:\s*([\d\s\xa0\.]+)"', html_block
    )
    for res_type, amount_str in matches:
        if res_type not in resource_map:
            continue
        idx = resource_map[res_type]
        amount_str = amount_str.replace("\xa0", "").replace(" ", "").replace(".", "")
        try:
            costs[idx] = int(amount_str)
        except ValueError:
            pass
    return costs


def splitBuildingBlocks(html):
    blocks = []
    pattern = r'<li class="building (\w+)[^"]*">((?:(?!<li class="building ).)+)</li>'
    for match in re.finditer(pattern, html, re.DOTALL):
        blocks.append({"type": match.group(1), "html": match.group(2)})
    return blocks


@configurator
def constructBuilding(session, event, stdin_fd, predetermined_input):
    """
    Parameters
    ----------
    session : ikabot.web.session.Session
    event : multiprocessing.Event
    stdin_fd: int
    predetermined_input : multiprocessing.managers.SyncManager.list
    """
    try:
        banner()

        print("City where to build:")
        city = chooseCity(session)
        banner()

        # list of free spaces in the selected city
        free_spaces = [
            buildings
            for buildings in city["position"]
            if buildings["building"] == "empty"
        ]

        # get a list of all the posible buildings that can be built
        buildings = []
        seen_ids = set()
        # different buildings can be built in different areas
        current_action_request = actionRequest
        type_spaces = ["sea", "land", "shore", "wall", "dockyard"]
        for type_space in type_spaces:
            free_spaces_of_type = [
                free_space
                for free_space in free_spaces
                if free_space["type"] == type_space
            ]
            # Iterate through available slots for this surface type until an active/unlocked slot is found.
            # Slots like position 13 require specific research (e.g., Bureaucracy) and will return empty HTML if locked.
            for free_space_of_type in free_spaces_of_type:
                params = {
                    "view": "buildingGround",
                    "cityId": city["id"],
                    "position": free_space_of_type["position"],
                    "backgroundView": "city",
                    "currentCityId": city["id"],
                    "actionRequest": actionRequest,
                    "ajax": "1",
                }
                buildings_response = session.post(params=params, noIndex=True)
                parsed_response = json.loads(buildings_response, strict=False)
                
                # Extract fresh actionRequest from the response
                try:
                    update_global = [item for item in parsed_response if item[0] == 'updateGlobalData']
                    if update_global:
                        current_action_request = update_global[0][1].get('actionRequest', actionRequest)
                    else:
                        current_action_request = actionRequest
                except Exception:
                    current_action_request = actionRequest
                
                buildings_response = parsed_response[1][1]
                if buildings_response == "":
                    continue
                
                html = buildings_response[1]
                blocks = splitBuildingBlocks(html)
                # Skip slot if the server returned empty content (e.g., locked slot or special restriction)
                if len(blocks) == 0:
                    continue

                for block in blocks:
                    btype = block["type"]
                    block_html = block["html"]
                    info_match = re.search(
                        r'<div title="(.+?)"\s*class="buildingimg .+?"\s*onclick="ajaxHandlerCall\(\'.*?buildingId=(\d+)&',
                        block_html,
                    )
                    if not info_match:
                        continue
                    
                    building_id = info_match.group(2)
                    building_name = info_match.group(1)
                    
                    if building_id in seen_ids:
                        continue
                    
                    seen_ids.add(building_id)
                    costs = parseBuildingCosts(block_html)
                    can_afford = all(
                        costs[i] <= city["availableResources"][i]
                        for i in range(len(materials_names))
                    )
                    
                    buildings.append(
                        {
                            "building": btype,
                            "name": building_name,
                            "buildingId": building_id,
                            "type": type_space,
                            "costs": costs,
                            "canAfford": can_afford,
                        }
                    )
                # Stop checking remaining slots of this surface type once build options are retrieved
                if len(blocks) > 0:
                    break

        if len(buildings) == 0:
            print("No building can be built.")
            enter()
            event.set()
            return

        # show list of buildings to the user with color coding
        while True:
            banner()
            print("What building do you want to build?\n")
            i = 0
            for building in buildings:
                i += 1
                color = bcolors.GREEN if building["canAfford"] else bcolors.RED
                print(
                    "({:d}) {}{}{}".format(
                        i, color, building["name"], bcolors.ENDC
                    )
                )
            print(
                "\n{}Green{} = can afford | {}Red{} = insufficient resources | (0) Exit".format(
                    bcolors.GREEN, bcolors.ENDC, bcolors.RED, bcolors.ENDC
                )
            )
            selected_building_index = read(min=0, max=i)
            if selected_building_index == 0:
                event.set()
                return
            selected_building = buildings[selected_building_index - 1]

            if not selected_building["canAfford"]:
                banner()
                print("{}Insufficient resources to build: {}{}".format(bcolors.RED, selected_building["name"], bcolors.ENDC))
                print("\nMissing resources:")
                any_missing = False
                for idx, cost in enumerate(selected_building["costs"]):
                    if cost == 0:
                        continue
                    have = city["availableResources"][idx]
                    if have >= cost:
                        continue
                    missing = cost - have
                    any_missing = True
                    print(
                        "- {}{}: {} {}(have: {}){}".format(
                            materials_names[idx],
                            bcolors.RED,
                            addThousandSeparator(missing),
                            bcolors.STONE,
                            addThousandSeparator(have),
                            bcolors.ENDC,
                        )
                    )
                if not any_missing:
                    print("You have enough resources for this building.")
                supplied = supplyBuildingResources(session, city, selected_building["costs"])
                if supplied is None:
                    continue
                break
            else:
                city = getCity(session.get(city_url + str(city["id"])))
                supplied = supplyBuildingResources(session, city, selected_building["costs"])
                if supplied is None:
                    continue
                break
        building = selected_building
        banner()
        print("{}\n".format(building["name"]))
        options = [
            position_id
            for position_id in city["position"]
            if position_id["building"] == "empty"
            and position_id["type"] == building["type"]
        ]
        if not options:
            print("No free position remains for this building.")
            enter()
            return
        if len(options) == 1:
            option = options[0]
        else:
            print("In which position do you want to build?\n")
            i = 0
            for option in options:
                i += 1
                print("({:d}) {}".format(i, option["position"]))
            selected_building_index = read(min=1, max=i)
            option = options[selected_building_index - 1]
            banner()

        print("Construction queued in background. You can return to the main menu.")
        return {"session": session, "city": city, "building": building,
                "option": option, "current_action_request": current_action_request,
                "supply": supplied}
    except KeyboardInterrupt:
        event.set()
        return


@task("constructBuilding")
def buildInBackground(session, city, building, option, current_action_request, supply):
    setInfoSignal(session, f"Build {building['name']} in {city.get('name', city['id'])}")
    if supply["origins"]:
        session.setStatus("Sending construction resources")
        sendResourcesNeeded(session, city["id"], supply["origins"], supply["missing"],
                            useFreighters=supply["useFreighters"])
    while True:
        city = getCity(session.get(city_url + str(city["id"])))
        slot = next((b for b in city["position"] if b["position"] == option["position"]), None)
        if slot is None or slot["building"] != "empty":
            sendToBot(session, "Construction cancelled: the selected position is no longer empty.")
            return
        required = _constructionRequirements(session, city, building["costs"])
        if getResourcesOverCapacity(city, required):
            sendToBot(session, "Construction cancelled: storage cannot hold costs plus wine reserve.")
            return
        if all(have >= need for have, need in zip(city["availableResources"], required)):
            break
        seconds = getMinimumWaitingTime(session)
        if seconds <= 0:
            sendToBot(session, "Construction cancelled: insufficient resources and no transport detected.")
            return
        session.setStatus("Waiting for construction resources to arrive")
        wait(seconds + 5)
    with activity(session):
        # Refresh the selected slot, costs and action token after transport waits.
        response = json.loads(session.post(params={
            "view": "buildingGround", "cityId": city["id"],
            "position": option["position"], "backgroundView": "city",
            "currentCityId": city["id"], "actionRequest": current_action_request,
            "ajax": "1",
        }, noIndex=True), strict=False)
        fresh_html = None
        for item in response:
            if item[0] == "updateGlobalData":
                current_action_request = item[1].get("actionRequest", current_action_request)
            elif item[0] == "changeView" and isinstance(item[1], list):
                fresh_html = item[1][1]
        fresh_block = next((block for block in splitBuildingBlocks(fresh_html or "")
                            if block["type"] == building["building"]), None)
        if fresh_block is None:
            sendToBot(session, "This building is no longer available in the selected position.")
            return
        fresh_costs = parseBuildingCosts(fresh_block["html"])
        city = getCity(session.get(city_url + str(city["id"])))
        slot = next((b for b in city["position"] if b["position"] == option["position"]), None)
        if slot is None or slot["building"] != "empty":
            sendToBot(session, "Construction cancelled: the selected position is no longer empty.")
            return
        required = _constructionRequirements(session, city, fresh_costs)
        if any(have < need for have, need in zip(city["availableResources"], required)):
            sendToBot(session, "Resources changed; construction cancelled to protect reserved stock. Please retry.")
            return
        # build it
        params = {
            "action": "BuildNewBuilding",
            "cityId": city["id"],
            "position": option["position"],
            "building": building["buildingId"],
            "backgroundView": "city",
            "currentCityId": city["id"],
            "templateView": "buildingGround",
            "actionRequest": current_action_request,
            "ajax": "1",
        }
        buildings_response = session.post(params=params, noIndex=True)
        response_data = json.loads(buildings_response, strict=False)
        msg = None
        for item in response_data:
            if item[0] == 'provideFeedback':
                for feedback in item[1]:
                    if feedback.get('text'):
                        msg = feedback['text']
                        break
                if msg:
                    break
        if not msg:
            msg = f"Construction request sent for {building['name']} in city {city['id']}."
        sendToBot(session, msg)
