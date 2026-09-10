import logging
from homeassistant.components.scene import Scene
from homeassistant.helpers.update_coordinator import CoordinatorEntity

_LOGGER = logging.getLogger(__name__)

# Scene "type" values that are handled by button.py instead (relays, gates, etc.)
# so we don't create a duplicate entity for the same underlying scene.
IO_SCENE_TYPES = {"genericio", "generic i/o", "i/o"}


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Crestron Scenes platform (lighting, shades, locks, climate, etc.)."""
    data = hass.data["crestron_home"][entry.entry_id]
    api, coordinator = data["api"], data["coordinator"]

    scenes_data = (coordinator.data or {}).get("scenes", {})
    scenes = scenes_data.get("scenes", []) if isinstance(scenes_data, dict) else []

    entities = []
    seen_ids = set()
    for s in scenes:
        scene_type = str(s.get("type", "")).strip().lower()
        scene_id = str(s.get("id"))

        # Skip relay/gate scenes - those are exposed as buttons instead
        if scene_type in IO_SCENE_TYPES:
            continue

        if scene_id and scene_id not in seen_ids:
            seen_ids.add(scene_id)
            entities.append(CrestronScene(coordinator, api, s["id"], s["name"]))

    async_add_entities(entities)


class CrestronScene(CoordinatorEntity, Scene):
    """Representation of a Crestron Home Scene."""

    def __init__(self, coordinator, api, scene_id, name):
        super().__init__(coordinator)
        self._api = api
        self._scene_id = scene_id
        self._attr_name = name
        self._attr_unique_id = f"crestron_scene_{scene_id}"

    @property
    def device_info(self):
        # All scenes share ONE device card ("Crestron Scenes") instead of
        # getting their own device each - same pattern used for the relay buttons.
        return {
            "identifiers": {("crestron_home", "scenes_group")},
            "name": "Crestron Scenes",
            "manufacturer": "Crestron",
            "model": "Scene Controller",
            "via_device": ("crestron_home", "processor"),
        }

    async def async_activate(self, **kwargs) -> None:
        """Activate/recall the scene on the Crestron Home processor."""
        _LOGGER.info("Recalling Crestron Scene ID %s (%s)", self._scene_id, self.name)
        await self._api.request("POST", f"/scenes/recall/{self._scene_id}", {})
