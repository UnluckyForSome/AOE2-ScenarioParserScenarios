"""
Battle Royale XS: 32x32 (1024 units), idle detection via XS arrays + trigger vars.

Architecture (same-cycle dispatch):
- XS maintains an idle queue (max 50) rebuilt every ~5 seconds by scanning
  all tracked units for no-target + no-movement.
- A looping "Dispatch" trigger (timer=1s, low index) calls XS
  dispatchNextBatch, which writes up to 50 idle unit ref IDs to ownership
  vars 0..49, and writes attack-move commands to vars 51..100 for units
  dispatched 3 cycles ago that are still idle.
- Triggers are ordered so the full pipeline evaluates in ONE trigger cycle:
  Dispatch (XS writes vars) -> IsIdle (looping, matches vars, activates
  Chance) -> Chance (fires same cycle, ownership change) -> AttackMoveCmd
  (looping, matches AM vars, issues attack-move).
- A high-frequency XS rule tracks per-unit kills via player kill-counter
  deltas and publishes a live top-30 leaderboard to objective triggers.

Refs: https://github.com/Divy1211/AoE2DE_UGC_Guide/tree/main/docs/general/xs/functions
      https://github.com/Divy1211/AoE2DE_UGC_Guide/tree/main/docs/scenarios/triggers
      https://ksneijders.github.io/AoE2ScenarioParser/api/index.html
      https://github.com/KSneijders/AoE2ScenarioParser/
"""

import io
import math
import os
import random
import sys
from pathlib import Path

# Import shared exclusion list from project root
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)
from NonCombatants import NON_COMBATANT_UNITS

from AoE2ScenarioParser.datasets.players import PlayerId
from AoE2ScenarioParser.datasets.terrains import TerrainId
from AoE2ScenarioParser.datasets.trigger_lists import Comparison, DiplomacyState
from AoE2ScenarioParser.datasets.units import UnitInfo
from AoE2ScenarioParser.scenarios.aoe2_de_scenario import AoE2DEScenario

# ---------------------------------------------------------------------------
# User-configurable constants
# ---------------------------------------------------------------------------

def _load_dotenv(dotenv_path: Path) -> None:
    """
    Minimal .env loader (KEY=VALUE, optional quotes).
    We keep this inline to avoid adding a hard dependency for a small script.
    """
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _load_env() -> None:
    # Prefer python-dotenv if available; otherwise fall back to minimal loader above.
    dotenv_candidates = [
        Path(__file__).resolve().parent / ".env",
        Path(_root).resolve() / ".env",
    ]
    try:
        from dotenv import load_dotenv  # type: ignore

        for p in dotenv_candidates:
            if p.exists():
                load_dotenv(p, override=False)
    except Exception:
        for p in dotenv_candidates:
            _load_dotenv(p)


def _require_env(name: str, *, hint: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required env var {name}. {hint}")
    return value


_load_env()

# Sensitive local path: read from env (via .env), not hardcoded.
SCENARIO_DIRECTORY = _require_env(
    "AOE2_SCENARIO_DIRECTORY",
    hint=(
        "Set it in a .env file (repo root or SubscriberBattleRoyale/.env), e.g. "
        'AOE2_SCENARIO_DIRECTORY="C:/.../resources/_common/scenario"'
    ),
)
SCENARIO_DIRECTORY_PATH = Path(SCENARIO_DIRECTORY).expanduser()

STARTING_SCENARIO_FILENAME = os.environ.get(
    "AOE2_STARTING_SCENARIO_FILENAME", "e3-p2-default.aoe2scenario"
).strip()
STARTING_SCENARIO_PATH = str(SCENARIO_DIRECTORY_PATH / STARTING_SCENARIO_FILENAME)

VERSION_FILE = Path(__file__).resolve().parent / "version.txt"
SUBSCRIBER_NAMES_FILE = Path(__file__).resolve().parent / "subscribers.txt"

USE_DEBUG_UNITS = False # If True: use debug units (just militia) for testing
USE_DEBUG_MAPONLY = False # If True: only map (terrain + units), no triggers or XS

SPAWNABLE_BATTLE_UNITS = [
    unit_info for unit_info in UnitInfo.non_gaia() if unit_info not in NON_COMBATANT_UNITS
]

MAP_SIZE = 480
CENTER_SIZE = 32
CENTER_OFFSET = (MAP_SIZE - CENTER_SIZE) // 2
MAP_CENTER = MAP_SIZE // 2

PLAYABLE_PLAYERS = [
    PlayerId.ONE, PlayerId.TWO, PlayerId.THREE, PlayerId.FOUR,
    PlayerId.FIVE, PlayerId.SIX, PlayerId.SEVEN, PlayerId.EIGHT,
]

SEQUENTIAL_OWNERSHIP_CHANCES = tuple(
    zip(PLAYABLE_PLAYERS, (13, 14, 17, 20, 25, 33, 50, 100))
)
DISPATCH_SLOT_COUNT = 50
IDLE_QUEUE_CAPACITY = DISPATCH_SLOT_COUNT
TRIGGER_VAR_IDLE_COUNT = DISPATCH_SLOT_COUNT
TRIGGER_VAR_ATTACK_MOVE_START = DISPATCH_SLOT_COUNT + 1
IDLE_QUEUE_REBUILD_COOLDOWN = 5
ATTACK_MOVE_DELAY_CYCLES = 3

LEADERBOARD_ENTRY_COUNT = 30
TRIGGER_VAR_LEADERBOARD_UNIT_START = (
    TRIGGER_VAR_ATTACK_MOVE_START + DISPATCH_SLOT_COUNT
)
TRIGGER_VAR_LEADERBOARD_KILLS_START = (
    TRIGGER_VAR_LEADERBOARD_UNIT_START + LEADERBOARD_ENTRY_COUNT
)
TRIGGER_VAR_LEADERBOARD_ALIVE_START = (
    TRIGGER_VAR_LEADERBOARD_KILLS_START + LEADERBOARD_ENTRY_COUNT
)
TRIGGER_VAR_BATTLE_WON = TRIGGER_VAR_LEADERBOARD_ALIVE_START + LEADERBOARD_ENTRY_COUNT
TRIGGER_VAR_CHAMPION_UNIT = TRIGGER_VAR_BATTLE_WON + 1

LEADERBOARD_TOP_SEPARATOR = "_" * 11
LEADERBOARD_SECTION_SEPARATOR = "¯" * 11

# ---------------------------------------------------------------------------
# XS script template  (use .format(); literal braces are doubled)
# ---------------------------------------------------------------------------
XS_TEMPLATE = """\
int gTrackedIds = -1;
int gPrevPos = -1;
int gIdleQueue = -1;
int gTrackedCount = 0;
int gIdleQueueSize = 0;
int gDispatchIdx = 0;
int gRebuildCooldown = 0;
int gDispatchAge = -1;
int gLastOwnershipCount = 0;
int gLastAMCount = 0;
int gReady = 0;

int gAlive = -1;
int gLastOwner = -1;
int gLastAttacker = -1;
int gUnitToIndex = -1;
int gKillTally = -1;
int gTopUnitLabel = -1;
int gTopKills = -1;
int gTopAlive = -1;
int gKillPrev = -1;
int gKillDelta = -1;
int gLeaderboardDirty = 0;
int gBattleWon = 0;
int gChampionUnitLabel = 0;

int killMatrixIndex(int src = 0, int tgt = 0) {{
    return (src * 9 + tgt);
}}

void updateKillMatrixDeltas() {{
    int src = 1;
    int tgt = 1;
    int _ = 0;
    for (src = 1; <= 8) {{
        int attr = 325 + src;
        for (tgt = 1; <= 8) {{
            int idx = killMatrixIndex(src, tgt);
            int prev = xsArrayGetInt(gKillPrev, idx);
            int now = 0 + xsPlayerAttribute(tgt, attr);
            int delta = now - prev;
            if (delta < 0) {{
                delta = 0;
            }}
            _ = xsArraySetInt(gKillPrev, idx, now);
            _ = xsArraySetInt(gKillDelta, idx, delta);
        }}
    }}
}}

int pickKillerOwnerByDelta(int deadOwner = 0, int preferredOwner = 0) {{
    if (preferredOwner >= 1 && preferredOwner <= 8) {{
        int prefIdx = killMatrixIndex(preferredOwner, deadOwner);
        if (xsArrayGetInt(gKillDelta, prefIdx) > 0) {{
            return (preferredOwner);
        }}
    }}
    int scan = 1;
    for (scan = 1; <= 8) {{
        int scanIdx = killMatrixIndex(scan, deadOwner);
        if (xsArrayGetInt(gKillDelta, scanIdx) > 0) {{
            return (scan);
        }}
    }}
    return (-1);
}}

void consumeKillDelta(int src = 0, int tgt = 0) {{
    int _ = 0;
    int idx = killMatrixIndex(src, tgt);
    int cur = xsArrayGetInt(gKillDelta, idx);
    if (cur > 0) {{
        _ = xsArraySetInt(gKillDelta, idx, cur - 1);
    }}
}}

int pickRandomTargeterByOwner(int deadUnitId = 0, int killerOwner = 0) {{
    if (killerOwner < 1 || killerOwner > 8) {{
        return (-1);
    }}
    int i = 0;
    int seen = 0;
    int pickedIdx = -1;
    for (i = 0; < gTrackedCount) {{
        int aId = xsArrayGetInt(gTrackedIds, i);
        if (xsDoesUnitExist(aId)) {{
            if (xsGetUnitOwner(aId) == killerOwner) {{
                if (xsGetUnitTargetUnitId(aId) == deadUnitId) {{
                    seen = seen + 1;
                    if (xsGetRandomNumberMax(seen) == 0) {{
                        pickedIdx = i;
                    }}
                }}
            }}
        }}
    }}
    return (pickedIdx);
}}

void updateLeaderboardSnapshot() {{
    int i = 0;
    int _ = 0;
    for (i = 0; < {leaderboard_size}) {{
        _ = xsArraySetInt(gTopUnitLabel, i, 0);
        _ = xsArraySetInt(gTopKills, i, 0);
        _ = xsArraySetInt(gTopAlive, i, 0);
    }}
    for (i = 0; < gTrackedCount) {{
        int unitLabel = i + 1;
        int kills = xsArrayGetInt(gKillTally, i);
        if (kills <= 0) {{
            continue;
        }}
        int pos = -1;
        int j = 0;
        for (j = 0; < {leaderboard_size}) {{
            int topK = xsArrayGetInt(gTopKills, j);
            int topU = xsArrayGetInt(gTopUnitLabel, j);
            if (kills > topK) {{
                pos = j;
                break;
            }}
            if (kills == topK && topU > 0 && unitLabel < topU) {{
                pos = j;
                break;
            }}
            if (topU <= 0) {{
                pos = j;
                break;
            }}
        }}
        if (pos >= 0) {{
            int k = 0;
            for (k = {leaderboard_last}; > pos) {{
                _ = xsArraySetInt(gTopKills, k, xsArrayGetInt(gTopKills, k - 1));
                _ = xsArraySetInt(gTopUnitLabel, k, xsArrayGetInt(gTopUnitLabel, k - 1));
                _ = xsArraySetInt(gTopAlive, k, xsArrayGetInt(gTopAlive, k - 1));
            }}
            _ = xsArraySetInt(gTopKills, pos, kills);
            _ = xsArraySetInt(gTopUnitLabel, pos, unitLabel);
            _ = xsArraySetInt(gTopAlive, pos, xsArrayGetInt(gAlive, i));
        }}
    }}
    for (i = 0; < {leaderboard_size}) {{
        xsSetTriggerVariable({var_lb_unit_start} + i, xsArrayGetInt(gTopUnitLabel, i));
        xsSetTriggerVariable({var_lb_kills_start} + i, xsArrayGetInt(gTopKills, i));
        xsSetTriggerVariable({var_lb_alive_start} + i, xsArrayGetInt(gTopAlive, i));
    }}
}}

string getPlayerColorOpen(int p = 0) {{
    if (p == 1) return ("<BLUE>");
    if (p == 2) return ("<RED>");
    if (p == 3) return ("<GREEN>");
    if (p == 4) return ("<YELLOW>");
    if (p == 5) return ("<AQUA>");
    if (p == 6) return ("<PURPLE>");
    if (p == 7) return ("<GREY>");
    if (p == 8) return ("<ORANGE>");
    return ("");
}}

{unit_name_lookup_fn}

void refreshKillTrackingTick() {{
    if (gReady == 0) {{
        return;
    }}
    if (gBattleWon > 0) {{
        return;
    }}
    updateKillMatrixDeltas();
    int i = 0;
    int _ = 0;
    int aliveCount = 0;
    int lastAliveLabel = 0;
    for (i = 0; < gTrackedCount) {{
        int u = xsArrayGetInt(gTrackedIds, i);
        int wasAlive = xsArrayGetInt(gAlive, i);
        int isAlive = 0;
        if (xsDoesUnitExist(u) && xsGetUnitHitpoints(u) > 0.0) {{
            isAlive = 1;
            aliveCount = aliveCount + 1;
            lastAliveLabel = i + 1;
            int targetId = xsGetUnitTargetUnitId(u);
            if (targetId > 0 && targetId < {unit_to_index_size}) {{
                int targetIdx = xsArrayGetInt(gUnitToIndex, targetId);
                if (targetIdx >= 0) {{
                    _ = xsArraySetInt(gLastAttacker, targetIdx, u);
                }}
            }}
            _ = xsArraySetInt(gLastOwner, i, xsGetUnitOwner(u));
        }}
        if (wasAlive > 0 && isAlive == 0) {{
            int deadOwnerId = xsArrayGetInt(gLastOwner, i);
            int lastAtkId = xsArrayGetInt(gLastAttacker, i);
            int preferredOwner = 0;
            if (lastAtkId > 0 && lastAtkId < {unit_to_index_size}) {{
                int atkIdx = xsArrayGetInt(gUnitToIndex, lastAtkId);
                if (atkIdx >= 0 && atkIdx < gTrackedCount) {{
                    preferredOwner = xsArrayGetInt(gLastOwner, atkIdx);
                }}
            }}
            int killerOwner = pickKillerOwnerByDelta(deadOwnerId, preferredOwner);
            if (killerOwner > 0) {{
                int killerIdx = pickRandomTargeterByOwner(u, killerOwner);
                if (killerIdx >= 0) {{
                    int oldK = xsArrayGetInt(gKillTally, killerIdx);
                    _ = xsArraySetInt(gKillTally, killerIdx, oldK + 1);
                    gLeaderboardDirty = 1;
                    xsChatData(getPlayerColorOpen(killerOwner) + getUnitName(killerIdx) + " killed " + getUnitName(i) + "!");
                }}
                consumeKillDelta(killerOwner, deadOwnerId);
            }}
            _ = xsArraySetInt(gLastAttacker, i, -1);
        }}
        _ = xsArraySetInt(gAlive, i, isAlive);
    }}
    if (gLeaderboardDirty > 0) {{
        updateLeaderboardSnapshot();
        gLeaderboardDirty = 0;
    }}
    if (aliveCount == 1) {{
        gBattleWon = 1;
        gChampionUnitLabel = lastAliveLabel;
        xsSetTriggerVariable({var_battle_won}, gBattleWon);
        xsSetTriggerVariable({var_champion_unit}, gChampionUnitLabel);
        xsDisableRuleGroup("killTracking");
    }}
}}

rule killTrackingTickRule
    active
    highFrequency
    group killTracking
{{
    refreshKillTrackingTick();
}}

void initTracker() {{
    gTrackedIds = xsArrayCreateInt({array_size}, -1, "trackedIds");
    gPrevPos = xsArrayCreateVector({array_size}, vector(0, 0, 0), "prevPos");
    gIdleQueue = xsArrayCreateInt({idle_queue_max}, -1, "idleQueue");
    gAlive = xsArrayCreateInt({array_size}, 0, "alive");
    gLastOwner = xsArrayCreateInt({array_size}, -1, "lastOwner");
    gLastAttacker = xsArrayCreateInt({array_size}, -1, "lastAttacker");
    gUnitToIndex = xsArrayCreateInt({unit_to_index_size}, -1, "unitToIdx");
    gKillTally = xsArrayCreateInt({array_size}, 0, "killTally");
    gTopUnitLabel = xsArrayCreateInt({leaderboard_size}, 0, "topUnit");
    gTopKills = xsArrayCreateInt({leaderboard_size}, 0, "topKills");
    gTopAlive = xsArrayCreateInt({leaderboard_size}, 0, "topAlive");
    gKillPrev = xsArrayCreateInt(81, 0, "killPrev");
    gKillDelta = xsArrayCreateInt(81, 0, "killDelta");
    gTrackedCount = {num_units};
    int _ = 0;
    gBattleWon = 0;
    gChampionUnitLabel = 0;
    {tracked_ids_lines}
    {unit_to_index_lines}
    int i = 0;
    for (i = 0; < gTrackedCount) {{
        int u = xsArrayGetInt(gTrackedIds, i);
        if (xsDoesUnitExist(u)) {{
            _ = xsArraySetVector(gPrevPos, i, xsGetUnitPosition(u));
            if (xsGetUnitHitpoints(u) > 0.0) {{
                _ = xsArraySetInt(gAlive, i, 1);
                _ = xsArraySetInt(gLastOwner, i, xsGetUnitOwner(u));
            }}
        }}
    }}
    updateKillMatrixDeltas();
    for (i = 0; < 81) {{
        _ = xsArraySetInt(gKillDelta, i, 0);
    }}
    xsSetTriggerVariable({var_battle_won}, 0);
    xsSetTriggerVariable({var_champion_unit}, 0);
    gReady = 1;
    gLeaderboardDirty = 1;
    updateLeaderboardSnapshot();
    gLeaderboardDirty = 0;
}}

void rebuildIdleQueue() {{
    int i = 0;
    int _ = 0;
    gIdleQueueSize = 0;
    for (i = 0; < gTrackedCount) {{
        int u = xsArrayGetInt(gTrackedIds, i);
        if (xsDoesUnitExist(u)) {{
            vector p = xsGetUnitPosition(u);
            vector old = xsArrayGetVector(gPrevPos, i);
            if (xsVectorGetX(p) == xsVectorGetX(old) && xsVectorGetY(p) == xsVectorGetY(old)) {{
                if (xsGetUnitTargetUnitId(u) <= 0) {{
                    if (gIdleQueueSize < {idle_queue_max}) {{
                        _ = xsArraySetInt(gIdleQueue, gIdleQueueSize, u);
                        gIdleQueueSize = gIdleQueueSize + 1;
                    }}
                }}
            }}
            _ = xsArraySetVector(gPrevPos, i, p);
        }}
    }}
    gDispatchIdx = 0;
    xsSetTriggerVariable({var_idle_count}, gIdleQueueSize);
}}

void dispatchNextBatch() {{
    if (gReady == 0) {{
        initTracker();
        return;
    }}
    int i = 0;
    int uid = 0;
    if (gBattleWon > 0) {{
        for (i = 0; < gLastAMCount) {{
            xsSetTriggerVariable({var_am_base} + i, -1);
        }}
        gLastAMCount = 0;
        for (i = 0; < gLastOwnershipCount) {{
            xsSetTriggerVariable(i, -1);
        }}
        gLastOwnershipCount = 0;
        return;
    }}

    gRebuildCooldown = gRebuildCooldown - 1;

    for (i = 0; < gLastAMCount) {{
        xsSetTriggerVariable({var_am_base} + i, -1);
    }}
    gLastAMCount = 0;

    if (gDispatchAge >= 0) {{
        gDispatchAge = gDispatchAge + 1;
        if (gDispatchAge >= {am_delay_cycles}) {{
            int amSlot = 0;
            for (i = 0; < gIdleQueueSize) {{
                uid = xsArrayGetInt(gIdleQueue, i);
                if (uid > 0) {{
                    if (xsDoesUnitExist(uid)) {{
                        if (xsGetUnitTargetUnitId(uid) <= 0) {{
                            xsSetTriggerVariable({var_am_base} + amSlot, uid);
                            amSlot = amSlot + 1;
                        }}
                    }}
                }}
            }}
            gLastAMCount = amSlot;
            gDispatchAge = -1;
        }}
    }}

    for (i = 0; < gLastOwnershipCount) {{
        xsSetTriggerVariable(i, -1);
    }}
    gLastOwnershipCount = 0;

    if (gDispatchIdx >= gIdleQueueSize && gRebuildCooldown <= 0) {{
        rebuildIdleQueue();
        gRebuildCooldown = {rebuild_cooldown};
    }}

    if (gDispatchIdx < gIdleQueueSize) {{
        int count = 0;
        for (i = 0; < {dispatch_slots}) {{
            if (gDispatchIdx < gIdleQueueSize) {{
                uid = xsArrayGetInt(gIdleQueue, gDispatchIdx);
                xsSetTriggerVariable(i, uid);
                count = count + 1;
                gDispatchIdx = gDispatchIdx + 1;
            }}
        }}
        gLastOwnershipCount = count;
        gDispatchAge = 0;
    }}
}}
"""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def ensure_utf8_stdout() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() in {"cp1252", "ascii"}:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def next_version(version_file: Path) -> int:
    try:
        version = int(version_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        version = 1
    version_file.write_text(str(version + 1), encoding="utf-8")
    return version


def xs_string_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def add_indexed_variables(trigger_manager, prefix: str, start_variable: int, count: int) -> None:
    for offset in range(count):
        trigger_manager.add_variable(f"{prefix}_{offset}", start_variable + offset)


def add_match_any_variable_condition(trigger, variable_start: int, slot_count: int, value: int) -> None:
    for slot in range(slot_count):
        if slot > 0:
            trigger.new_condition.or_()
        trigger.new_condition.variable_value(
            variable=variable_start + slot,
            quantity=value,
            comparison=Comparison.EQUAL,
        )


def configure_map(map_manager) -> None:
    map_manager.map_size = MAP_SIZE
    x1, y1 = CENTER_OFFSET, CENTER_OFFSET
    x2, y2 = CENTER_OFFSET + CENTER_SIZE - 1, CENTER_OFFSET + CENTER_SIZE - 1
    map_manager.set_elevation(elevation=20, x1=x1, y1=y1, x2=x2, y2=y2)
    for tile in map_manager.get_square_1d(x1=x1, y1=y1, x2=x2, y2=y2):
        tile.terrain_id = TerrainId.SHALLOWS_YELLOW


def configure_players(player_manager) -> None:
    player_manager.active_players = len(PLAYABLE_PLAYERS)
    for player_id in PLAYABLE_PLAYERS:
        other_players = [
            other_id for other_id in PLAYABLE_PLAYERS if other_id != player_id
        ]
        player_manager.players[player_id].set_player_diplomacy(other_players, DiplomacyState.ENEMY)


def _compute_unit_positions(map_size: int, n_units: int, margin: float | None = None) -> list[tuple[int, int]]:
    """
    Compute integer tile positions for units, laid out on an approximately square grid which
    fills as much of the map as possible, with units equidistant in x/y.
    """
    if n_units <= 0:
        return []

    cols = math.ceil(math.sqrt(n_units))
    rows = math.ceil(n_units / cols)

    # If margin is None, we choose an integer grid step and compute margins from it.
    # This guarantees *exactly equal* spacing between adjacent units (no mixed 7/8 gaps).
    #
    # We "aim" for margin ≈ step/2, but allow the margin to shift by ~1 tile if needed
    # to keep a single global step that works for both axes.
    gaps_x = max(0, cols - 1)
    gaps_y = max(0, rows - 1)

    if margin is None:
        k = max(gaps_x, gaps_y)
        if k == 0:
            step = 1
        else:
            # Try largest possible step first.
            step = (map_size - 1) // k
            # Require margins to be at least ~ step/2 on both axes.
            # Use integer arithmetic: 2*margin >= step-1 allows half-tile rounding slack.
            while step > 1:
                rem_x = (map_size - 1) - step * gaps_x
                rem_y = (map_size - 1) - step * gaps_y
                if rem_x < 0 or rem_y < 0:
                    step -= 1
                    continue
                left_x = rem_x // 2
                left_y = rem_y // 2
                if (2 * left_x >= step - 1) and (2 * left_y >= step - 1):
                    break
                step -= 1

        start_x = ((map_size - 1) - step * gaps_x) // 2 if gaps_x > 0 else (map_size - 1) // 2
        start_y = ((map_size - 1) - step * gaps_y) // 2 if gaps_y > 0 else (map_size - 1) // 2

        def coord(i: int, count: int, start: int) -> int:
            if count <= 1:
                return start
            return start + i * step
    else:
        # Fixed margin mode (kept for manual tweaking)
        min_coord = margin
        max_coord = (map_size - 1) - margin
        if max_coord < min_coord:
            min_coord = 0
            max_coord = map_size - 1

        def coord(i: int, count: int, start: int) -> int:
            if count <= 1:
                return int(round((min_coord + max_coord) / 2))
            t = i / (count - 1)
            return int(round(min_coord + t * (max_coord - min_coord)))

    positions: list[tuple[int, int]] = []
    for k in range(n_units):
        r = k // cols
        c = k % cols
        if r >= rows:
            break
        x = coord(c, cols, start_x if margin is None else 0)
        y = coord(r, rows, start_y if margin is None else 0)
        # Clamp to map bounds just in case
        x = max(0, min(map_size - 1, x))
        y = max(0, min(map_size - 1, y))
        positions.append((x, y))
    return positions


def place_units(unit_manager) -> list[int]:
    """Place invisible anchors and grid battle units. Returns ref IDs of battle units."""
    top_y = MAP_SIZE - 0.5
    for idx, player_id in enumerate(PLAYABLE_PLAYERS):
        unit_manager.add_unit(
            player=player_id,
            unit_const=UnitInfo.INVISIBLE_OBJECT_A.ID,
            x=0.5 + idx, y=top_y,
        )

    unit_ref_ids: list[int] = []
    n_units = CENTER_SIZE * CENTER_SIZE
    for tile_x, tile_y in _compute_unit_positions(MAP_SIZE, n_units, margin=None):
        player = random.choice(PLAYABLE_PLAYERS)
        unit_const = (
            UnitInfo.MILITIA.ID
            if USE_DEBUG_UNITS
            else random.choice(SPAWNABLE_BATTLE_UNITS).ID
        )
        u = unit_manager.add_unit(
            player=player,
            unit_const=unit_const,
            x=tile_x + 0.5,
            y=tile_y + 0.5,
            rotation=random.uniform(0, 2 * math.pi),
        )
        unit_ref_ids.append(u.reference_id)
    return unit_ref_ids


def build_xs_script(unit_ref_ids: list[int], unit_names: list[str]) -> str:
    num_units = len(unit_ref_ids)
    max_ref_id = max(unit_ref_ids)
    tracked_ids_lines = "\n    ".join(
        f"_ = xsArraySetInt(gTrackedIds, {i}, {rid});"
        for i, rid in enumerate(unit_ref_ids)
    )
    unit_to_index_lines = "\n    ".join(
        f"_ = xsArraySetInt(gUnitToIndex, {rid}, {i});"
        for i, rid in enumerate(unit_ref_ids)
    )
    name_if_lines = "\n    ".join(
        f'if (idx == {i}) return ("{xs_string_literal(name)}");'
        for i, name in enumerate(unit_names)
    )
    unit_name_lookup_fn = (
        "string getUnitName(int idx = 0) {\n"
        "    " + name_if_lines + "\n"
        '    return ("???");\n'
        "}"
    )
    return XS_TEMPLATE.format(
        array_size=num_units,
        idle_queue_max=IDLE_QUEUE_CAPACITY,
        num_units=num_units,
        tracked_ids_lines=tracked_ids_lines,
        unit_to_index_lines=unit_to_index_lines,
        unit_to_index_size=max_ref_id + 1,
        var_idle_count=TRIGGER_VAR_IDLE_COUNT,
        var_am_base=TRIGGER_VAR_ATTACK_MOVE_START,
        dispatch_slots=DISPATCH_SLOT_COUNT,
        rebuild_cooldown=IDLE_QUEUE_REBUILD_COOLDOWN,
        am_delay_cycles=ATTACK_MOVE_DELAY_CYCLES,
        leaderboard_size=LEADERBOARD_ENTRY_COUNT,
        leaderboard_last=LEADERBOARD_ENTRY_COUNT - 1,
        var_lb_unit_start=TRIGGER_VAR_LEADERBOARD_UNIT_START,
        var_lb_kills_start=TRIGGER_VAR_LEADERBOARD_KILLS_START,
        var_lb_alive_start=TRIGGER_VAR_LEADERBOARD_ALIVE_START,
        var_battle_won=TRIGGER_VAR_BATTLE_WON,
        var_champion_unit=TRIGGER_VAR_CHAMPION_UNIT,
        unit_name_lookup_fn=unit_name_lookup_fn,
    )


def load_unit_names(names_file: Path, count: int) -> list[str]:
    lines = names_file.read_text(encoding="utf-8").splitlines()
    names = [line.strip() for line in lines if line.strip()]
    if len(names) < count:
        raise ValueError(f"Need {count} names but {names_file} only has {len(names)}")
    return names[:count]


def add_triggers(trigger_manager, unit_ref_ids: list[int], unit_names: list[str]) -> None:
    num_units = len(unit_ref_ids)
    add_indexed_variables(trigger_manager, "DispatchSlot", 0, DISPATCH_SLOT_COUNT)
    trigger_manager.add_variable("IdleCount", TRIGGER_VAR_IDLE_COUNT)
    add_indexed_variables(
        trigger_manager,
        "AMSlot",
        TRIGGER_VAR_ATTACK_MOVE_START,
        DISPATCH_SLOT_COUNT,
    )
    add_indexed_variables(
        trigger_manager,
        "LBUnit",
        TRIGGER_VAR_LEADERBOARD_UNIT_START,
        LEADERBOARD_ENTRY_COUNT,
    )
    add_indexed_variables(
        trigger_manager,
        "LBKills",
        TRIGGER_VAR_LEADERBOARD_KILLS_START,
        LEADERBOARD_ENTRY_COUNT,
    )
    add_indexed_variables(
        trigger_manager,
        "LBAlive",
        TRIGGER_VAR_LEADERBOARD_ALIVE_START,
        LEADERBOARD_ENTRY_COUNT,
    )
    trigger_manager.add_variable("BattleWon", TRIGGER_VAR_BATTLE_WON)
    trigger_manager.add_variable("ChampionUnit", TRIGGER_VAR_CHAMPION_UNIT)

    # --- Rename (low index, fires once) ---
    rename_tr = trigger_manager.add_trigger("Rename Units")
    rename_tr.looping = False
    for i, ref_id in enumerate(unit_ref_ids):
        rename_tr.new_effect.change_object_name(
            selected_object_ids=[ref_id],
            message=f"{unit_names[i]} ({i + 1})",
        )

    # --- Dispatch (low index — XS writes vars before any other trigger reads them) ---
    dispatch_tr = trigger_manager.add_trigger("Dispatch Batch")
    dispatch_tr.new_condition.timer(timer=1)
    dispatch_tr.looping = True
    dispatch_tr.new_effect.script_call(message="dispatchNextBatch")

    # --- Small utility triggers first, so they stay near the top of the trigger list ---
    top_sep_order = 2001
    title_order = 2000
    sep_names_order = 1999

    top_sep = trigger_manager.add_trigger("KillLeaderboardTopSep")
    top_sep.enabled = True
    top_sep.looping = True
    top_sep.display_as_objective = 1
    top_sep.display_on_screen = 1
    top_sep.description_order = top_sep_order
    top_sep.short_description = LEADERBOARD_TOP_SEPARATOR
    top_sep.description = " "

    sep_names = trigger_manager.add_trigger("KillLeaderboardSepNames")
    sep_names.enabled = True
    sep_names.looping = True
    sep_names.display_as_objective = 1
    sep_names.display_on_screen = 1
    sep_names.description_order = sep_names_order
    sep_names.short_description = LEADERBOARD_SECTION_SEPARATOR
    sep_names.description = " "

    title = trigger_manager.add_trigger("TopKillersTitle")
    title.enabled = True
    title.looping = True
    title.header = 1
    title.display_as_objective = 1
    title.display_on_screen = 1
    title.description_order = title_order
    title.short_description = "Top Killers"
    title.description = title.short_description

    line_deactivator = trigger_manager.add_trigger("TopKillersLineDeactivator")
    line_deactivator.enabled = True
    line_deactivator.looping = True

    display_refresh_tr = trigger_manager.add_trigger("Display Refresh")
    display_refresh_tr.new_condition.timer(timer=3)
    display_refresh_tr.looping = True
    display_refresh_tr.new_effect.clear_instructions()

    show_idle_trigger_ids: list[int] = []
    for n in range(IDLE_QUEUE_CAPACITY + 1):
        tr = trigger_manager.add_trigger(f"ShowIdleCount_{n}")
        show_idle_trigger_ids.append(tr.trigger_id)
        tr.enabled = False
        tr.looping = False
        tr.new_condition.variable_value(
            variable=TRIGGER_VAR_IDLE_COUNT,
            quantity=n,
            comparison=Comparison.EQUAL,
        )
        tr.new_effect.display_instructions(
            message=f"Idle Queue: {n}/{IDLE_QUEUE_CAPACITY}",
            display_time=4,
            source_player=PlayerId.ONE,
        )

    for tid in show_idle_trigger_ids:
        display_refresh_tr.new_effect.activate_trigger(trigger_id=tid)

    shutdown_tr = trigger_manager.add_trigger("BattleWonShutdown")
    shutdown_tr.enabled = True
    shutdown_tr.looping = False
    shutdown_tr.new_condition.variable_value(
        variable=TRIGGER_VAR_BATTLE_WON,
        quantity=1,
        comparison=Comparison.EQUAL,
    )

    # --- IsIdle per unit (looping+enabled, reads ownership vars same-cycle) ---
    print(f"  Building is-idle triggers for {num_units} units...")
    idle_match_trigger_ids: list[int] = []

    for i, ref_id in enumerate(unit_ref_ids):
        tr = trigger_manager.add_trigger(f"IsIdle_U{i}")
        idle_match_trigger_ids.append(tr.trigger_id)
        tr.enabled = True
        tr.looping = True
        add_match_any_variable_condition(tr, 0, DISPATCH_SLOT_COUNT, ref_id)

    print(f"  Done ({num_units} triggers).")

    # --- Chance per unit (positioned after IsIdle — evaluates same cycle) ---
    print(f"  Building chance triggers for {num_units} units...")
    ownership_roll_trigger_ids_by_unit: list[list[int]] = []

    for i, ref_id in enumerate(unit_ref_ids):
        chance_ids: list[int] = []
        for target_player, chance_pct in SEQUENTIAL_OWNERSHIP_CHANCES:
            tr = trigger_manager.add_trigger(f"Chance_U{i}_P{target_player}")
            chance_ids.append(tr.trigger_id)
            tr.enabled = False
            tr.looping = False
            tr.new_condition.chance(quantity=chance_pct)
            tr.new_effect.change_ownership(
                source_player=PlayerId.GAIA,
                target_player=target_player,
                selected_object_ids=[ref_id],
            )

        for tid in chance_ids:
            for other_id in chance_ids:
                trigger_manager.triggers[tid].new_effect.deactivate_trigger(
                    trigger_id=other_id
                )

        ownership_roll_trigger_ids_by_unit.append(chance_ids)

    print(f"  Done ({num_units * len(SEQUENTIAL_OWNERSHIP_CHANCES)} triggers).")

    # --- Wire IsIdle -> Chance (second pass, now that Chance trigger IDs exist) ---
    print(f"  Wiring IsIdle -> Chance effects...")
    for i in range(num_units):
        is_idle_tr = trigger_manager.triggers[idle_match_trigger_ids[i]]
        for chance_tid in ownership_roll_trigger_ids_by_unit[i]:
            is_idle_tr.new_effect.activate_trigger(trigger_id=chance_tid)

    # --- AttackMoveCmd per unit (looping+enabled, reads AM vars same-cycle) ---
    print(f"  Building attack-move triggers for {num_units} units...")
    attack_move_command_trigger_ids: list[int] = []

    for i, ref_id in enumerate(unit_ref_ids):
        tr = trigger_manager.add_trigger(f"AttackMoveCmd_U{i}")
        attack_move_command_trigger_ids.append(tr.trigger_id)
        tr.enabled = True
        tr.looping = True
        add_match_any_variable_condition(
            tr,
            TRIGGER_VAR_ATTACK_MOVE_START,
            DISPATCH_SLOT_COUNT,
            ref_id,
        )

        tr.new_effect.attack_move(
            selected_object_ids=[ref_id],
            source_player=PlayerId.GAIA,
            location_x=MAP_CENTER,
            location_y=MAP_CENTER,
        )

    print(f"  Done ({num_units} triggers).")

    # --- Ordered Top Killers lines (rank-based so the on-screen order matches the leaderboard) ---
    print(
        f"  Building ordered Top Killers lines for {num_units} units across "
        f"{LEADERBOARD_ENTRY_COUNT} ranks..."
    )
    line_trigger_ids: list[int] = []

    for rank in range(LEADERBOARD_ENTRY_COUNT):
        unit_var = TRIGGER_VAR_LEADERBOARD_UNIT_START + rank
        kill_var = TRIGGER_VAR_LEADERBOARD_KILLS_START + rank
        alive_var = TRIGGER_VAR_LEADERBOARD_ALIVE_START + rank
        line_order = sep_names_order - 1 - rank

        for i, name in enumerate(unit_names):
            label = i + 1

            selector = trigger_manager.add_trigger(f"TopKillersSelect_R{rank + 1}_U{i}")
            selector.enabled = True
            selector.looping = True
            selector.new_condition.variable_value(
                variable=unit_var,
                quantity=label,
                comparison=Comparison.EQUAL,
            )

            line = trigger_manager.add_trigger(f"TopKillersLine_R{rank + 1}_U{i}")
            line_trigger_ids.append(line.trigger_id)
            line.enabled = False
            line.looping = False
            line.display_as_objective = 1
            line.display_on_screen = 1
            line.description_order = line_order
            line.new_condition.variable_value(
                variable=alive_var,
                quantity=0,
                comparison=Comparison.EQUAL,
            )
            line.short_description = (
                f"- #{rank + 1} {name}: <Variable {kill_var}> kills"
            )
            line.description = " "

            selector.new_effect.activate_trigger(trigger_id=line.trigger_id)

    for tid in line_trigger_ids:
        line_deactivator.new_effect.deactivate_trigger(trigger_id=tid)

    print(f"  Done ({num_units * LEADERBOARD_ENTRY_COUNT * 2 + 3} triggers).")

    shutdown_tr.new_effect.deactivate_trigger(trigger_id=dispatch_tr.trigger_id)
    for trigger_id in idle_match_trigger_ids:
        shutdown_tr.new_effect.deactivate_trigger(trigger_id=trigger_id)
    for trigger_id in attack_move_command_trigger_ids:
        shutdown_tr.new_effect.deactivate_trigger(trigger_id=trigger_id)
    shutdown_tr.new_effect.deactivate_trigger(trigger_id=line_deactivator.trigger_id)
    shutdown_tr.new_effect.deactivate_trigger(trigger_id=display_refresh_tr.trigger_id)

    print("  Building champion announcement + shutdown triggers...")

    for i, name in enumerate(unit_names):
        label = i + 1
        tr = trigger_manager.add_trigger(f"ChampionAnnounce_U{i}")
        tr.enabled = True
        tr.looping = False
        tr.new_condition.variable_value(
            variable=TRIGGER_VAR_BATTLE_WON,
            quantity=1,
            comparison=Comparison.EQUAL,
        )
        tr.new_condition.variable_value(
            variable=TRIGGER_VAR_CHAMPION_UNIT,
            quantity=label,
            comparison=Comparison.EQUAL,
        )
        tr.new_effect.clear_instructions()
        tr.new_effect.display_instructions(
            message=f"Congratulations {name}, you are the Champion!",
            display_time=20,
            source_player=PlayerId.GAIA
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    ensure_utf8_stdout()
    version = next_version(VERSION_FILE)
    output_path = str(SCENARIO_DIRECTORY_PATH / f"SubscriberBattleRoyale - v{version}.aoe2scenario")

    # Until player.personality is implamented in AOE2ScenarioParser, we need to use a default scenario with e3-p2.ai selected
    # scenario = AoE2DEScenario.from_default()
    scenario = AoE2DEScenario.from_file(STARTING_SCENARIO_PATH)

    print("Configuring map...")
    configure_map(scenario.map_manager)

    print("Configuring players...")
    configure_players(scenario.player_manager)

    print("Placing units...")
    unit_ref_ids = place_units(scenario.unit_manager)
    print(f"  Placed {len(unit_ref_ids)} battle units.")

    if not USE_DEBUG_MAPONLY:
        print("Loading unit names...")
        unit_names = load_unit_names(SUBSCRIBER_NAMES_FILE, len(unit_ref_ids))

        print("Building XS script...")
        xs_manager = scenario.xs_manager
        xs_manager.initialise_xs_trigger()
        xs_manager.xs_trigger.enabled = True
        xs_manager.add_script(xs_string=build_xs_script(unit_ref_ids, unit_names))

        print("Building triggers...")
        add_triggers(scenario.trigger_manager, unit_ref_ids, unit_names)
    else:
        print("USE_DEBUG_MAPONLY: skipping triggers and XS.")

    scenario.write_to_file(output_path)
    print(f"Scenario written (v{version}): {output_path}")


if __name__ == "__main__":
    main()
