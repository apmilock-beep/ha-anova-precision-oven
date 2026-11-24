"""Tests for steam/humidity control and keeping a running cook.

The stage lists below were captured from a real Anova Precision Oven v1
(hardware 120V1, firmware 2.1.16) on 2026-11-24 while the Anova iOS app
ran each cook; ids are as the app sent them, the cooker id is not included.
"""
from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.anova_precision_oven.climate import AnovaOvenClimate
from custom_components.anova_precision_oven.const import CMD_APO_START
from custom_components.anova_precision_oven.cook import (
    CookValidationError,
    adjust_stages,
    stage_steam,
)
from custom_components.anova_precision_oven.sensor import _steam_setpoint
from homeassistant.components.climate import HVACAction

# App cook: 300 °F dry, rear element, steam 35 % (steam-percentage), no timer
APP_STEAM_COOK: list[dict[str, Any]] = [
    {
        "steamGenerators": {"mode": "steam-percentage", "steamPercentage": {"setpoint": 35}},
        "heatingElements": {"rear": {"on": True}, "top": {"on": False}, "bottom": {"on": False}},
        "temperatureBulbs": {"dry": {"setpoint": {"celsius": 148.89, "fahrenheit": 300}}, "mode": "dry"},
        "vent": {"open": False},
        "type": "preheat",
        "id": "ios-f1b986e3-ddb1-47a7-b595-956087db1ae1",
        "title": "",
        "userActionRequired": False,
        "fan": {"speed": 100},
    },
    {
        "userActionRequired": False,
        "steamGenerators": {"mode": "steam-percentage", "steamPercentage": {"setpoint": 35}},
        "heatingElements": {"rear": {"on": True}, "top": {"on": False}, "bottom": {"on": False}},
        "type": "cook",
        "id": "ios-0940dc1b-b55f-4df4-87a1-2f372e873fe6",
        "temperatureBulbs": {"mode": "dry", "dry": {"setpoint": {"fahrenheit": 300, "celsius": 148.89}}},
        "title": "",
        "fan": {"speed": 100},
        "vent": {"open": False},
    },
]

# App cook: sous vide (wet bulb) 130 °F, relative humidity 100, 5 min timer
APP_SOUS_VIDE_COOK: list[dict[str, Any]] = [
    {
        "steamGenerators": {"mode": "relative-humidity", "relativeHumidity": {"setpoint": 100}},
        "heatingElements": {"rear": {"on": True}, "top": {"on": False}, "bottom": {"on": False}},
        "temperatureBulbs": {"wet": {"setpoint": {"fahrenheit": 130, "celsius": 54.44}}, "mode": "wet"},
        "vent": {"open": False},
        "type": "preheat",
        "id": "ios-6f32d95c-ec66-45a0-b15e-15a1c6a55c30",
        "title": "",
        "userActionRequired": False,
        "fan": {"speed": 100},
    },
    {
        "userActionRequired": False,
        "steamGenerators": {"mode": "relative-humidity", "relativeHumidity": {"setpoint": 100}},
        "heatingElements": {"rear": {"on": True}, "top": {"on": False}, "bottom": {"on": False}},
        "temperatureBulbs": {"wet": {"setpoint": {"fahrenheit": 130, "celsius": 54.44}}, "mode": "wet"},
        "type": "cook",
        "id": "ios-77155461-a2c5-4468-b115-c97ad2b893df",
        "timer": {"initial": 300},
        "title": "",
        "fan": {"speed": 100},
        "vent": {"open": False},
    },
]

PREHEAT_ID = APP_STEAM_COOK[0]["id"]
COOK_ID = APP_STEAM_COOK[1]["id"]
SV_COOK_ID = APP_SOUS_VIDE_COOK[1]["id"]


def _without(stage: dict[str, Any], *keys: str) -> dict[str, Any]:
    out = copy.deepcopy(stage)
    for key in keys:
        out.pop(key, None)
    return out


# --- cook.py -------------------------------------------------------------


def test_stage_steam_reads_both_modes():
    assert stage_steam(APP_STEAM_COOK[1]) == 35
    assert stage_steam(APP_SOUS_VIDE_COOK[1]) == 100
    assert stage_steam({"type": "cook"}) == 0
    assert stage_steam({"steamGenerators": {"mode": "idle"}}) == 0


def test_steam_change_during_preheat_keeps_everything_else():
    out = adjust_stages(APP_STEAM_COOK, PREHEAT_ID, steam=50)
    assert [s["id"] for s in out] == [PREHEAT_ID, COOK_ID]
    for new, old in zip(out, APP_STEAM_COOK):
        assert new["steamGenerators"] == {
            "mode": "steam-percentage",
            "steamPercentage": {"setpoint": 50},
        }
        assert _without(new, "steamGenerators") == _without(old, "steamGenerators")
    # input left alone
    assert stage_steam(APP_STEAM_COOK[0]) == 35


def test_temperature_change_mid_cook_drops_preheat_and_keeps_steam():
    out = adjust_stages(APP_STEAM_COOK, COOK_ID, temperature_c=176.67)
    assert [s["type"] for s in out] == ["cook"]  # no preheat re-run
    (cook,) = out
    assert cook["temperatureBulbs"]["dry"]["setpoint"] == {
        "celsius": 176.67,
        "fahrenheit": 350.01,
    }
    assert cook["steamGenerators"] == APP_STEAM_COOK[1]["steamGenerators"]
    assert _without(cook, "temperatureBulbs") == _without(
        APP_STEAM_COOK[1], "temperatureBulbs"
    )


def test_steam_change_mid_cook_carries_timer_over():
    out = adjust_stages(
        APP_SOUS_VIDE_COOK, SV_COOK_ID, steam=80, timer_remaining_s=254
    )
    (cook,) = out
    assert cook["timer"] == {"initial": 254}
    assert cook["steamGenerators"] == {
        "mode": "relative-humidity",
        "relativeHumidity": {"setpoint": 80},
    }
    assert cook["temperatureBulbs"] == APP_SOUS_VIDE_COOK[1]["temperatureBulbs"]
    assert APP_SOUS_VIDE_COOK[1]["timer"] == {"initial": 300}


def test_steam_off_in_dry_mode_removes_steam_and_keeps_fan_setting():
    stages = copy.deepcopy(APP_STEAM_COOK)
    stages[1]["fan"] = {"speed": 60}
    stages[1]["heatingElements"] = {"top": {"on": True}, "bottom": {"on": False}, "rear": {"on": False}}
    (cook,) = adjust_stages(stages, COOK_ID, steam=0)
    assert "steamGenerators" not in cook
    assert cook["fan"] == {"speed": 60}


def test_adding_steam_locks_fan_high():
    stages = copy.deepcopy(APP_STEAM_COOK)
    for stage in stages:
        stage.pop("steamGenerators")
        stage["fan"] = {"speed": 40}
    out = adjust_stages(stages, PREHEAT_ID, steam=35)
    assert all(s["fan"] == {"speed": 100} for s in out)


def test_temperature_and_steam_together_and_limits():
    out = adjust_stages(APP_STEAM_COOK, PREHEAT_ID, temperature_c=200, steam=20)
    assert all(stage_steam(s) == 20 for s in out)
    assert all(s["temperatureBulbs"]["dry"]["setpoint"]["celsius"] == 200 for s in out)
    with pytest.raises(CookValidationError):
        adjust_stages(APP_STEAM_COOK, COOK_ID, steam=101)
    with pytest.raises(CookValidationError):
        adjust_stages(APP_SOUS_VIDE_COOK, SV_COOK_ID, temperature_c=120)
    assert adjust_stages(APP_STEAM_COOK, "gone", steam=10) is None


def test_later_stage_with_its_own_steam_is_left_alone():
    first = copy.deepcopy(APP_STEAM_COOK[1])
    second = copy.deepcopy(APP_STEAM_COOK[1])
    second["id"] = "second"
    second["steamGenerators"]["steamPercentage"]["setpoint"] = 70
    out = adjust_stages([first, second], COOK_ID, steam=10)
    assert [stage_steam(s) for s in out] == [10, 70]


# --- sensor ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("steam", "expected"),
    [
        ({"mode": "steam-percentage", "steamPercentage": {"setpoint": 35}}, 35),
        ({"mode": "relative-humidity", "relativeHumidity": {"setpoint": 100, "current": 82}}, 100),
        ({"mode": "idle"}, 0),
        ({}, None),
    ],
)
def test_humidity_setpoint_sensor_reads_every_mode(steam, expected):
    assert _steam_setpoint({"nodes": {"steamGenerators": steam}}) == expected


# --- climate entity ---------------------------------------------------------


def _state(stages, active_id, *, timer=None, steam_nodes=None, bulb_mode="dry"):
    return {
        "version": 1,
        "state": {"mode": "cook", "temperatureUnit": "F"},
        "cook": {
            "cookId": "ios-cook",
            "stages": copy.deepcopy(stages),
            "activeStageId": active_id,
        },
        "nodes": {
            "temperatureBulbs": {
                "mode": bulb_mode,
                bulb_mode: {
                    "current": {"celsius": 100.0},
                    "setpoint": copy.deepcopy(
                        stages[-1]["temperatureBulbs"][bulb_mode]["setpoint"]
                    ),
                },
            },
            "timer": timer or {"mode": "idle", "initial": 0, "current": 0},
            "steamGenerators": steam_nodes
            or {"mode": "steam-percentage", "steamPercentage": {"setpoint": 35}},
        },
    }


def _climate(state):
    coordinator = MagicMock()
    coordinator.get_device_info.return_value = {"name": "Oven", "type": "oven_v1"}
    coordinator.get_device_state.return_value = state
    coordinator.devices = {"dev": {"state": state}}
    coordinator.send_command = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()
    coordinator.stop_when_timer_done = set()
    entity = AnovaOvenClimate(coordinator, "dev")
    return entity, coordinator


def _sent_stages(coordinator):
    command, payload = coordinator.send_command.call_args.args[1:3]
    assert command == CMD_APO_START
    assert set(payload) == {"stages", "cookId", "originSource"}
    return payload["stages"]


async def test_climate_reports_steam_and_preheating():
    entity, _ = _climate(_state(APP_STEAM_COOK, PREHEAT_ID))
    assert entity.target_humidity == 35
    assert entity.current_humidity is None  # steam-percentage reports no RH
    assert entity.hvac_action == HVACAction.PREHEATING
    entity, _ = _climate(_state(APP_STEAM_COOK, COOK_ID))
    assert entity.hvac_action == HVACAction.HEATING


async def test_climate_set_humidity_mid_cook_keeps_temperature():
    entity, coordinator = _climate(_state(APP_STEAM_COOK, COOK_ID))
    await entity.async_set_humidity(50)
    (cook,) = _sent_stages(coordinator)
    assert stage_steam(cook) == 50
    assert cook["temperatureBulbs"] == APP_STEAM_COOK[1]["temperatureBulbs"]
    assert cook["heatingElements"] == APP_STEAM_COOK[1]["heatingElements"]


async def test_climate_set_temperature_mid_cook_keeps_steam_and_timer():
    stages = copy.deepcopy(APP_STEAM_COOK)
    stages[1]["timer"] = {"initial": 600}
    state = _state(
        stages, COOK_ID, timer={"mode": "running", "initial": 600, "current": 412}
    )
    entity, coordinator = _climate(state)
    await entity.async_set_temperature(temperature=176.67)
    (cook,) = _sent_stages(coordinator)
    assert stage_steam(cook) == 35
    assert cook["timer"] == {"initial": 412}
    assert cook["temperatureBulbs"]["dry"]["setpoint"]["celsius"] == 176.67


async def test_climate_off_humidity_used_at_turn_on():
    state = _state(APP_STEAM_COOK, COOK_ID)
    state["state"]["mode"] = "idle"
    state.pop("cook")
    entity, coordinator = _climate(state)
    with patch.object(entity, "async_write_ha_state"):
        await entity.async_set_humidity(40)
    assert entity.target_humidity == 40
    coordinator.send_command.assert_not_called()
    await entity.async_turn_on()
    preheat, cook = _sent_stages(coordinator)
    assert stage_steam(preheat) == stage_steam(cook) == 40
    assert cook["heatingElements"]["rear"] == {"on": True}
    assert entity._pending_humidity is None


async def test_climate_turn_on_after_sous_vide_uses_dry_default():
    state = _state(
        APP_SOUS_VIDE_COOK,
        SV_COOK_ID,
        bulb_mode="wet",
        steam_nodes={"mode": "relative-humidity", "relativeHumidity": {"setpoint": 100}},
    )
    state["state"]["mode"] = "idle"
    state.pop("cook")
    entity, coordinator = _climate(state)
    await entity.async_turn_on()
    preheat, cook = _sent_stages(coordinator)
    assert cook["temperatureBulbs"]["mode"] == "dry"
    assert cook["temperatureBulbs"]["dry"]["setpoint"]["celsius"] == 175
    assert "steamGenerators" not in cook  # RH 100 is not carried into dry


async def test_climate_off_after_sous_vide_shows_what_turn_on_uses():
    state = _state(
        APP_SOUS_VIDE_COOK,
        SV_COOK_ID,
        bulb_mode="wet",
        steam_nodes={"mode": "relative-humidity", "relativeHumidity": {"setpoint": 100, "current": 84}},
    )
    state["state"]["mode"] = "idle"
    state.pop("cook")
    entity, _ = _climate(state)
    assert entity.native_target_temperature == 175
    assert entity.target_humidity == 0
    assert entity.current_humidity == 84


async def test_climate_off_after_steam_cook_shows_last_settings():
    state = _state(APP_STEAM_COOK, COOK_ID)
    state["state"]["mode"] = "idle"
    state.pop("cook")
    entity, _ = _climate(state)
    assert entity.native_target_temperature == 148.89
    assert entity.target_humidity == 35


async def test_extra_attributes_do_not_shadow_target_humidity():
    entity, _ = _climate(_state(APP_STEAM_COOK, COOK_ID))
    # "humidity" is Home Assistant's target-humidity attribute
    assert "humidity" not in entity.extra_state_attributes
    assert entity.extra_state_attributes["steam_mode"] == "steam-percentage"
