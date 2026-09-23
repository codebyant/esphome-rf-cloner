# Add an RF device

Group the commands for one piece of equipment — a ceiling fan, a gate, a projector — under a
single Home Assistant device, in the right area.

An RF device lives **only in Home Assistant**. Creating, editing or deleting one never touches the
bridge, never relearns anything and never changes a command's identity. It is purely how the
commands are presented.

## Why bother

Without an RF device, every command is a loose button with no device. With one, you get:

- one device page per piece of equipment, with all its buttons under **Controls**;
- the area set once, for every command at once;
- icons suggested from the device's type — fan chevrons for a fan, arrows for a shutter;
- a clean entity list, grouped by equipment.

![Entities grouped under the Bedroom-Fan device](../../assets/screenshots/08-command-entities.png)

## 1. Open the form

**Settings → Devices & services → RF Cloner Bridge**, then **Add an RF device** at the top of the
page.

![The integration page, with the Add an RF device button](../../assets/screenshots/03-integration-page.png)

## 2. Describe the equipment

![The Add an RF device form: name, type and area](../../assets/screenshots/04-add-rf-device.png)

| Field | What to put |
|---|---|
| **Name** | What you call the equipment: `Bedroom Fan`, `Garage Gate`. Must be unique on this bridge |
| **Type** | Fan, Gate, Light, Shutter or cover, TV, Air conditioner, Generic or Other |
| **Area** | Where it is. Optional |

**Submit**. The device appears on the integration page as its own sub-entry, empty until commands
are assigned to it.

### About the type

The type is **presentation only**. It labels the device and steers the icon suggestions. It does
not turn the equipment into a `fan` or `cover` entity with on/off state, because an RF remote
sends blind — Home Assistant cannot know whether the fan actually turned on. Buttons stay honest
about that.

### About the area

The area is applied **once**, when the device is created. After that it is yours: move the device
to another area in Home Assistant and the integration never moves it back.

## 3. Put commands on it

Two ways:

- **New commands** — choose the RF device in the **RF device** field when you
  [learn a command](learn-a-command.md).
- **Existing commands** — move them, one at a time, through
  [Edit or delete a command](edit-move-or-delete-a-command.md#move-a-command-to-another-rf-device).

Once commands are on it, the RF device's page shows them all:

![The Bedroom-Fan device page with ten command buttons](../../assets/screenshots/07-rf-device-page.png)

## Edit an RF device

On the integration page, the **gear icon** on the RF device's row opens **Edit RF device**: name,
type and area. Nothing on the bridge changes, and every command keeps its entity id and history.

The pencil icon on the device row below it is Home Assistant's own device editor — use that for the
area after creation, or to rename the device the way Home Assistant renames any device.

## Delete an RF device

The **three-dot menu** on the RF device's row, then **Delete**.

This is **organisational only**:

- the RF device and its Home Assistant device disappear;
- its commands become **Unassigned** and keep their buttons, ids, icons and history;
- nothing is deleted from the bridge.

To erase a command's waveform from the bridge, that is a separate, deliberate step —
[Delete a command](edit-move-or-delete-a-command.md#delete-a-command).

## RF devices have no icon

Home Assistant's device registry has no icon field, so an RF device cannot carry one. The type and
the per-command icons are what this version offers. The device page's header shows the
integration's icon instead.
