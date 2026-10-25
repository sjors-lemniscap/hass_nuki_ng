from __future__ import annotations

import logging
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .nuki import NukiCoordinator
from .constants import DOMAIN, PLATFORMS

OPENER_TYPE = 1
LOCK_TYPE = 0

_LOGGER = logging.getLogger(__name__)


# ------------------------------------------------------
#  Home Assistant Setup
# ------------------------------------------------------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    data = entry.as_dict()["data"]
    _LOGGER.debug(f"async_setup_entry: {data}")

    coordinator = NukiCoordinator(hass, entry, data)
    entry.runtime_data = coordinator

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry):
    coordinator = entry.runtime_data
    await coordinator.unload()
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    entry.runtime_data = None
    return True


def _service_targets(hass: HomeAssistant, call: ServiceCall):
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    device_ids = set(cv.ensure_list(call.data.get("device_id", [])))
    for entity_id in cv.ensure_list(call.data.get("entity_id", [])):
        entity = entity_reg.async_get(entity_id)
        if entity is not None and entity.device_id:
            device_ids.add(entity.device_id)
    targets = []
    for device_id in device_ids:
        device = device_reg.async_get(device_id)
        if device is None:
            continue
        for domain, value in device.identifiers:
            if domain != DOMAIN:
                continue
            for entry in hass.config_entries.async_entries(DOMAIN):
                coordinator = entry.runtime_data
                if coordinator is None:
                    continue
                bridge_id = ((coordinator.data or {}).get("bridge_info") or {}).get("ids", {}).get("hardwareId")
                if value == bridge_id:
                    targets.append((coordinator, None))
                elif value in coordinator.data.get("devices", {}):
                    targets.append((coordinator, value))
    return targets


def _register_service(hass: HomeAssistant, name: str, handler):
    async def handler_(call: ServiceCall):
        targets = _service_targets(hass, call)
        if not targets:
            _LOGGER.warning(f"service call {name}: no Nuki device targeted")
        for coordinator, dev_id in targets:
            _LOGGER.debug(f"service call: {name} for {dev_id}")
            await handler(coordinator, dev_id, call.data)

    hass.services.async_register(DOMAIN, name, handler_)


async def _bridge_reboot(coordinator, dev_id, data):
    await coordinator.do_reboot()


async def _bridge_fwupdate(coordinator, dev_id, data):
    await coordinator.do_fwupdate()


async def _bridge_delete_callback(coordinator, dev_id, data):
    await coordinator.do_delete_callback(data.get("callback"))


async def _execute_action(coordinator, dev_id, data):
    if dev_id is None:
        await coordinator.action_for_devices(data.get("action"))
    else:
        await coordinator.action(dev_id, data.get("action"))


async def async_setup(hass: HomeAssistant, config) -> bool:
    _register_service(hass, "bridge_reboot", _bridge_reboot)
    _register_service(hass, "bridge_fwupdate", _bridge_fwupdate)
    _register_service(hass, "bridge_delete_callback", _bridge_delete_callback)
    _register_service(hass, "execute_action", _execute_action)
    return True


# ------------------------------------------------------
#  Nuki Base Entities
# ------------------------------------------------------

class NukiEntity(CoordinatorEntity):
    """Base entity for Nuki locks and openers."""

    def __init__(self, coordinator, device_id: str):
        super().__init__(coordinator)
        self.device_id = device_id

    # ---- ID + Name helpers -------------------------------------

    def set_id(self, prefix: str, suffix: str):
        self.id_prefix = prefix
        self.id_suffix = suffix

    def set_name(self, name: str):
        self._attr_name_suffix = name

    @property
    def name_suffix(self):
        return self._attr_name_suffix

    @property
    def get_name(self):
        return "Nuki %s" % (self.data.get("name", self.device_id))

    @property
    def name(self) -> str:
        return "%s %s" % (self.get_name, self.name_suffix)

    @property
    def unique_id(self) -> str:
        return "nuki-%s-%s" % (self.device_id, self.id_suffix)

    # ---- Data Access --------------------------------------------

    @property
    def available(self):
        if "nukiId" not in self.data:
            return False
        return super().available

    @property
    def data(self) -> dict:
        return self.coordinator.device_data(self.device_id)

    @property
    def last_state(self) -> dict:
        return self.data.get("lastKnownState", {})

    @property
    def model(self) -> str:
        if self.coordinator.is_lock(self.device_id):
            return "Nuki Smart Lock"
        if self.coordinator.is_opener(self.device_id):
            return "Nuki Opener"
        return "Nuki Device"

    # ---- FIXED device_info --------------------------------------

    @property
    def device_info(self):
        """
        Correct HA-compatible device definition.
        Ensures:
        - identifiers use (DOMAIN, id)
        - via_device only set if bridge_id exists
        """
        # Bridge Hardware-ID (may be None)
        bridge_id = self.coordinator.info_data().get("ids", {}).get("hardwareId")

        info = {
            "identifiers": {(DOMAIN, self.device_id)},
            "name": self.get_name,
            "manufacturer": "Nuki",
            "model": self.model,
            "sw_version": self.data.get("firmwareVersion"),
        }

        # Add bridging only when valid
        if bridge_id:
            info["via_device"] = (DOMAIN, bridge_id)

        return info


# ------------------------------------------------------
#  Nuki Bridge Entity
# ------------------------------------------------------

class NukiBridge(CoordinatorEntity):
    """Represents the Nuki Bridge device."""

    def set_id(self, suffix: str):
        self.id_suffix = suffix

    def set_name(self, name: str):
        self.name_suffix = name

    @property
    def name(self) -> str:
        return "Nuki Bridge %s" % (self.name_suffix)

    @property
    def unique_id(self) -> str:
        return "nuki-bridge-%s-%s" % (self.get_id, self.id_suffix)

    @property
    def data(self) -> dict:
        return self.coordinator.data.get("bridge_info", {})

    @property
    def get_id(self):
        return self.data.get("ids", {}).get("hardwareId")

    @property
    def device_info(self):
        """Correct HA-compliant Bridge info."""
        model = (
            "Hardware Bridge"
            if self.data.get("bridgeType", 1) == 1
            else "Software Bridge"
        )
        versions = self.data.get("versions", {})

        return {
            "identifiers": {(DOMAIN, self.get_id)},
            "name": "Nuki Bridge",
            "manufacturer": "Nuki",
            "model": model,
            "sw_version": versions.get("firmwareVersion"),
        }


# ------------------------------------------------------
#  Advanced Opener Entity
# ------------------------------------------------------

class NukiOpenerRingSuppressionEntity(NukiEntity):

    SUP_RING = 4
    SUP_RTO = 2
    SUP_CM = 1

    @property
    def entity_category(self):
        return EntityCategory.CONFIG

    @property
    def doorbellSuppression(self):
        return self.coordinator.info_field(
            self.device_id, 0, "openerAdvancedConfig", "doorbellSuppression"
        )

    async def update_doorbell_suppression(self, new_value):
        await self.coordinator.update_config(
            self.device_id,
            "openerAdvancedConfig",
            dict(doorbellSuppression=new_value),
        )

