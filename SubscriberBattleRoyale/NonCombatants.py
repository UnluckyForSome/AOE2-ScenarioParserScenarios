"""Shared list of non-combatant units to exclude from military/spawn pools. Run as script to print included/excluded."""
from AoE2ScenarioParser.datasets.units import UnitInfo

# Add or remove units here; both SubscriberBattleRoyale and the print-out below use this.
NON_COMBATANT_UNITS = (
    set(UnitInfo.vils())
    | {
        UnitInfo.FISHING_SHIP,
        UnitInfo.TRANSPORT_SHIP,
        UnitInfo.TRADE_COG,
        UnitInfo.TRADE_CART_EMPTY,
        UnitInfo.TRADE_CART_FULL,
        UnitInfo.SHEEP,
        UnitInfo.TURKEY,
        UnitInfo.CART,
        UnitInfo.DONKEY,
        UnitInfo.RELIC_CART,
        UnitInfo.MOVEABLE_MAP_REVEALER_A,
        UnitInfo.ALFRED_THE_ALPACA,
        UnitInfo.ALPACA,
        UnitInfo.CAPYBARA,
        UnitInfo.CHICKEN_A,
        UnitInfo.CHICKEN_B,
        UnitInfo.CHICKEN_C,
        UnitInfo.COW_A,
        UnitInfo.COW_B,
        UnitInfo.COW_C,
        UnitInfo.COW_D,
        UnitInfo.GOAT_UNCONVERTIBLE,
        UnitInfo.HORSE_A,
        UnitInfo.HORSE_B,
        UnitInfo.HORSE_C,
        UnitInfo.HORSE_D,
        UnitInfo.HORSE_E,
        UnitInfo.HUNNIC_HORSE,
        UnitInfo.LLAMA_A,
        UnitInfo.LLAMA_B,
        UnitInfo.PIG,
        UnitInfo.WATER_BUFFALO,
        UnitInfo.CAMEL,
        UnitInfo.JUNK,
        UnitInfo.CUMAN_DISABLED,
        UnitInfo.FOOD_WOOD_GOLD_TRICKLE,
        UnitInfo.GOAT,
        UnitInfo.GOOSE,
        UnitInfo.HINT_OBJECT,
        UnitInfo.INVISIBLE_OBJECT_A,
        UnitInfo.INVISIBLE_OBJECT_B,
        UnitInfo.INVISIBLE_OBJECT_C,
        UnitInfo.INVISIBLE_OBJECT_D,
        UnitInfo.INVISIBLE_OBJECT_E,
        UnitInfo.MERCHANT_SHIP,
        UnitInfo.MERCHANT,
        UnitInfo.OX_CART,
        UnitInfo.OX_WAGON,
        UnitInfo.KING,
        UnitInfo.QUEEN,
        UnitInfo.VMDL,
        UnitInfo.TORCH_A_CONVERTABLE,
        UnitInfo.TORCH_B_CONVERTABLE,
        # Monk-line non-combatants (no attack)
        UnitInfo.MONK,
        UnitInfo.MISSIONARY,
        UnitInfo.PRIEST,
        UnitInfo.PAGAN_PRIEST,
        UnitInfo.WARRIOR_PRIEST,
        UnitInfo.MONK_WITH_RELIC,
        UnitInfo.MONK_WITH_TURKISH_RELIC,
        UnitInfo.PRIEST_WITH_RELIC,
        UnitInfo.WARRIOR_PRIEST_WITH_RELIC,
        UnitInfo.PAGAN_PRIEST_WITH_RELIC,
        UnitInfo.MISSIONARY_WITH_RELIC,
        UnitInfo.SIEGE_TOWER,
        UnitInfo.TREASURE_CHEST,
        UnitInfo.WHALING_SHIP,
        UnitInfo.TRANSPORT_SHIP_ANTIQUITY,
        UnitInfo.MONUMENT_RESOURCE_ENABLER,
        UnitInfo.MOVEABLE_MAP_REVEALER_B,
        UnitInfo.CONDOTTIERO_PLACEHOLDER,
        UnitInfo.RFARC,
        UnitInfo.TREBUCHET,
        UnitInfo.TREBUCHET_PACKED
    }
)

# Backwards-compatible alias for other modules
IGNORE = NON_COMBATANT_UNITS

INCLUDED = [u for u in UnitInfo.non_gaia() if u not in NON_COMBATANT_UNITS]
EXCLUDED = [u for u in UnitInfo.non_gaia() if u in NON_COMBATANT_UNITS]


if __name__ == "__main__":
    print("Included (combatants):")
    for u in sorted(INCLUDED, key=lambda x: x.name):
        print(f"  {u.name:40} ID={u.ID:5}  Icon={u.ICON_ID:4}  DeadID={u.DEAD_ID:5}")
    print(f"\nTotal included: {len(INCLUDED)}")
    print("Excluded (non-combatants):")
    for u in sorted(EXCLUDED, key=lambda x: x.name):
        print(f"  {u.name:40} ID={u.ID:5}  Icon={u.ICON_ID:4}  DeadID={u.DEAD_ID:5}")
    print(f"\nTotal excluded: {len(EXCLUDED)}\n")
