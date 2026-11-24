# Anova Precision Oven for Home Assistant

Home Assistant integration for the Anova Precision Oven (APO) using Anova's [official developer API](https://developer.anovaculinary.com/docs/devices/wifi/oven-commands). Real-time telemetry via WebSocket (`wss://devices.anovaculinary.io`), plus cook control.

> **Fork.** This is a fork of [wrongdoug/ha-anova-precision-oven](https://github.com/wrongdoug/ha-anova-precision-oven) that adds full cook control (`start_cook`, `start_custom_cook`), a cook-milestone event entity, power/stage/fault sensors, and command error reporting. The action design borrows from [andr83/hacs-anova-oven](https://github.com/andr83/hacs-anova-oven), reimplemented on the official Personal Access Token API. It keeps the `anova_precision_oven` domain, so it drops in over the original with the same config entry and entity IDs.

> **Provided as-is. No warranty. No expectation of maintenance, support, or future updates.** This is a personal project shared in case it's useful to someone else. You are responsible for verifying correct behavior -- particularly when automating against a physical cooking appliance.

## Scope

Full telemetry for the oven, plus control:

- **Climate entity:** quick on/off and target temperature (simple dry cook).
- **`start_cook` action:** dry or sous vide, heating elements, fan, steam, vent, rack, probe target or timer, preheat, wait-for-start. See [Actions](#actions).
- **`start_custom_cook` action:** any multi-stage recipe from raw stages.
- **Lamp switch.**

`start_cook` builds v1 stages. On a v2 oven, use `start_custom_cook` with v2 stages (untested).

### Supported

- Anova Precision Oven v1 (tested)
- Anova Precision Oven v2 (untested; reads should work, the cook-start path sends extra fields the API docs suggest v2 requires)

### Not Supported

Anova Precision Cooker (APC sous vide circulator). That's a different product with a different API -- use the built-in [Anova integration](https://www.home-assistant.io/integrations/anova) in HA core.

## Entities

### Sensors (read-only)

| Entity | Description |
|---|---|
| Dry temperature | Current dry bulb temperature |
| Wet temperature | Current wet bulb temperature |
| Probe temperature | Meat probe temperature (when connected) |
| Humidity | Current relative humidity |
| Timer | Cook timer remaining (seconds) |
| Mode | `idle` / `preheat` / `cook` |
| Dry / Wet / Probe temperature setpoint | Target temperatures from whatever cook is running |
| Humidity setpoint | Steam setpoint: steam percentage in a dry cook, relative humidity in sous vide (`steam_mode` attribute says which); 0 with no steam |
| Fan speed | Current fan speed percentage |
| Rack position | Current rack position |
| Timer start type | `Immediately` / `When preheated` / etc. |
| Heating elements | `Top` / `Bottom` / `Rear` / combinations / `All` / `Off` |
| Firmware / Hardware version | Diagnostic |
| Cook session | Current cook ID or `idle`; `stages` attribute holds the running cook's raw stages (not recorded) |
| Active stage | Index of the running stage; `stage_count`, `stage_type`, `stage_title` attributes |
| Cook time | Seconds since the cook started |
| Steam mode | `idle` / `relative-humidity` / `steam-percentage` |
| Heating power | Total watts drawn by elements, evaporator and boiler |
| Top / Bottom / Rear element, Evaporator, Boiler power | Per-part watts (disabled by default) |

### Binary Sensors (6, read-only)

| Entity | Description |
|---|---|
| Connectivity | Online / offline |
| Door | Open / closed |
| Water tank | Empty (problem) + `descale_required` attribute |
| Vent | Open / closed |
| Descale required | Problem: the boiler needs descaling |
| Fault | Problem: any failed/overheated part the oven reports; `faults` attribute lists them |

### Event (1)

`event.<oven>_cook` fires once per cook for each milestone:

| Event type | When |
|---|---|
| `preheated` | The oven leaves its preheat stage (or, for a cook without one, reaches within 1 °C of its setpoint) |
| `probe_target_reached` | Probe temperature reaches its setpoint |
| `timer_finished` | A cook timer runs out (the timer resets and the oven keeps heating unless `stop_when_done`) |
| `cook_ended` | The oven goes back to idle |

A restart mid-cook doesn't re-fire milestones already reached. Trigger on it with `event.received` (or a state trigger on the event entity plus a condition on `event_type`).

### Climate (1)

`OFF` and `HEAT` HVAC modes, target temperature 25-250 C, target humidity 0-100 % (steam). See [Climate Entity Behavior](#climate-entity-behavior).

### Switch (1)

Lamp on/off.

## Climate Entity Behavior

The climate entity works like a thermostat for the oven (temperature step 5°, humidity step 5 %):

- **Target humidity is the oven's steam setting.** In a dry cook it is the steam percentage (what the Anova app calls steam, sent as `steam-percentage`); in a sous vide (wet bulb) cook it is relative humidity. `current_humidity` is the measured relative humidity, which the oven reports in sous vide but not with steam percentage.
- **Off:** changing the temperature or humidity only stores it, and the entity shows the temperature and steam that turning on will use. Turning on starts a dry cook like the app's manual cook: rear element, fan 100 %, a preheat stage, no timer, the stored (or last) temperature and steam percentage. After a sous vide cook, turning on uses 175 °C and no steam unless other values were chosen.
- **Cooking:** changing the temperature or the humidity re-sends the running cook from the active stage on, with only that value changed. Elements, fan, vent, the other value and the remaining timer are kept; a preheat stage and the cook stage after it move together, while later stages with their own settings keep them. A cook already past preheat is re-sent without its preheat stage. Adding steam locks the fan on high, as the app does. Anova's API has no "adjust" command, so this is a fresh cook as far as the oven is concerned (its cook ID changes).
- **Preheating:** v1 ovens report mode `cook` during the preheat stage, so `hvac_action` reads the active stage and shows `preheating` until the cook stage starts.
- The measured relative humidity is in the standard `current_humidity` attribute (the old extra `humidity` attribute is gone, as it hid the target humidity), and `steam_mode` says which steam mode the oven is in.
- `turn_off` sends `CMD_APO_STOP`.

## Actions

Both actions target the oven's climate entity and **replace any cook in progress**. Temperatures are in your Home Assistant unit system.

### `anova_precision_oven.start_cook`

| Field | Default | Notes |
|---|---|---|
| `temperature` | required | Dry: 77–482 °F / 25–250 °C (356 °F / 180 °C max with only the bottom element). Sous vide: up to 212 °F / 100 °C |
| `mode` | `dry` | `dry` or `sous_vide` (wet bulb) |
| `heating_elements` | `[rear]` | Any of `top`, `bottom`, `rear`; at least one |
| `fan_speed` | `100` | 0–100 %. Only applies with top/bottom elements and no steam: the rear element or any steam locks the fan at 100 %, as in the Anova app |
| `steam` | none | Dry: steam percentage. Sous vide: relative humidity (default 100) |
| `vent_open` | `false` | |
| `rack_position` | `3` | 1–5 |
| `probe_temperature` | none | Probe target; can't combine with `duration` |
| `duration` | none | Timer; can't combine with `probe_temperature` |
| `preheat` | `true` | Adds a preheat stage; the cook stage and its timer start once preheated |
| `wait_for_start` | `false` | After preheating, wait for a tap on the oven before cooking |
| `stop_when_done` | `false` | Turn the oven off when the timer ends. The oven itself keeps holding temperature after its timer, so Home Assistant sends the stop (it must be running then) |
| `title` | `""` | |

```yaml
action: anova_precision_oven.start_cook
target:
  entity_id: climate.anova_precision_oven
data:
  temperature: 350
  heating_elements: [top, rear]
  fan_speed: 80
  steam: 20
  duration:
    minutes: 25
```

### `anova_precision_oven.start_custom_cook`

Starts a cook from raw stages. To save an app recipe as a script: start it in the Anova app, copy the **Cook session** sensor's `stages` attribute, and paste it as `stages`.

```yaml
action: anova_precision_oven.start_custom_cook
target:
  entity_id: climate.anova_precision_oven
data:
  stages: [...]
```

Errors the oven sends back for a command are raised in Home Assistant instead of failing silently.

## Development

```
pip install pytest-homeassistant-custom-component
pytest
```

The tests use an idle state and two cooks (35 % steam, and sous vide with a timer) captured from a real v1 oven, as the Anova app sent them.

## Installation

Requires Home Assistant 2026.11 or newer.

### HACS

1. In HACS, open the three-dot menu and select **Custom repositories**
2. Add `https://github.com/wrongdoug/ha-anova-precision-oven` as an **Integration**
3. Install "Anova Precision Oven" and restart HA

### Manual

Copy `custom_components/anova_precision_oven/` into your HA config's `custom_components/` directory and restart.

## Configuration

**Settings > Devices & Services > Add Integration > Anova Precision Oven**, then paste your Personal Access Token (starts with `anova-`).

Tokens are documented in Anova's [authentication guide](https://developer.anovaculinary.com/docs/devices/wifi/authentication). Max 10 PATs per account. WebSocket connections time out after 30 minutes of inactivity; the integration auto-reconnects.

## License

MIT. See `LICENSE`.
