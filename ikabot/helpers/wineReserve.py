"""Shared wine reserve for construction and resource transport."""

import math
from decimal import Decimal

WINE_INDEX = 1
WINE_RESERVE = 10000
WINE_RESERVE_HOURS = 24


def getWineReserve(session, city, waiting_seconds=0):
    from ikabot.function.tavernManager import TavernManager

    consumption = Decimal(str(city.get("wineConsumptionPerHour", 0)))
    tavern = next((b for b in city["position"] if b["building"] == "tavern"), None)
    if consumption <= 0 and tavern is not None:
        data = TavernManager(session)._get_tavern_data(city, tavern, force_refresh=True)
        if not data or not data["consumption_values"]:
            raise ValueError("Cannot determine maximum tavern consumption; wine spending blocked.")
        # A depleted city may shortly resume serving at 100%.
        consumption = Decimal(max(wine for level, wine in data["consumption_values"]))
    consumption = max(Decimal(0), consumption)
    reserve = max(WINE_RESERVE, math.ceil(consumption * WINE_RESERVE_HOURS))
    return reserve + math.ceil(consumption * Decimal(str(max(0, waiting_seconds))) / 3600)

