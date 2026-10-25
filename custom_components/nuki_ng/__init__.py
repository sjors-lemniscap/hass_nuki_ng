from __future__ import annotations

import logging
from homeassistant.core import HomeAssistant
from homeassistant.helpers import service
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .nuki import NukiCoordinator
from .constants import DOMAIN, PLATFORMS

OPENER_TYPE = 1
LOCK_TYPE = 0

LEGACY_IDENTIFIER_DOMAINS = ("id", "web_id")

_LOGGER = logging.getLogger(__name__)


def web_device_id(device_id) -> str:
    return f"web_{device_id}"


# ------------------------------------------------------
#  Home Assistant Setup
# ------------------------------------------------------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    data = entry.as_dict()["data"]
    _LOGGER.debug(f"async_setup_entry: {data}")

    _migrate_devices(hass, entry)

    coordinator = NukiCoordinator(hass, entry, data)
    entry.runtime_data = coordinator

    await coordinator.async_config_entry_first_refresh()
    _register_devices(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry, device) -> bool:
    coordinator = entry.runtime_data
    for domain, value in device.identifiers:
        if domain != DOMAIN:
            continue
        if coordinator is not None and (value in coordinator.data.get("devices", {}) or value == coordinator.bridge_id()):
            return False
    return True


def _migrate_devices(hass: HomeAssistant, entry: ConfigEntry):
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
        if not any(domain in LEGACY_IDENTIFIER_DOMAINS for domain, _ in device.identifiers):
            continue
        identifiers = set()
        for domain, value in device.identifiers:
            if domain == "id":
                identifiers.add((DOMAIN, value))
            elif domain == "web_id":
                identifiers.add((DOMAIN, web_device_id(value)))
            else:
                identifiers.add((domain, value))
        existing = None
        for identifier in identifiers:
            if hasattr(device_reg, "async_get_device_by_identifier"):
                found = device_reg.async_get_device_by_identifier(identifier, entry.entry_id)
            else:
                found = device_reg.async_get_device(identifiers={identifier})
            if found is not None and found.id != device.id:
                existing = found
                break
        if existing is None:
            _LOGGER.info(f"Migrating device {device.id} to identifiers {identifiers}")
            device_reg.async_update_device(device.id, new_identifiers=identifiers)
            continue
        _LOGGER.info(f"Merging legacy device {device.id} into {existing.id}")
        for entity in er.async_entries_for_device(entity_reg, device.id, include_disabled_entities=True):
            entity_reg.async_update_entity(entity.entity_id, device_id=existing.id)
        device_reg.async_remove_device(device.id)


def _register_devices(hass: HomeAssistant, entry: ConfigEntry, coordinator: NukiCoordinator):
    device_reg = dr.async_get(hass)
    bridge_device_id = None
    bridge_info = coordinator.bridge_info()
    if coordinator.bridge_id():
        versions = bridge_info.get("versions") or {}
        bridge = device_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, coordinator.bridge_id())},
            name="Nuki Bridge",
            manufacturer="Nuki",
            model="Hardware Bridge" if bridge_info.get("bridgeType", 1) == 1 else "Software Bridge",
            sw_version=versions.get("firmwareVersion"),
        )
        bridge_device_id = bridge.id
    for dev_id, data in coordinator.data.get("devices", {}).items():
        args = dict(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, dev_id)},
            name="Nuki %s" % (data.get("name", dev_id)),
            manufacturer="Nuki",
            model=coordinator.model(dev_id),
            sw_version=data.get("firmwareVersion"),
        )
        if bridge_device_id is not None:
            args["via_device_id"] = bridge_device_id
        device = device_reg.async_get_or_create(**args)
        if data.get("web_auth"):
            device_reg.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, web_device_id(dev_id))},
                name="Nuki Web API",
                manufacturer="Nuki",
                via_device_id=device.id,
            )


async def async_unload_entry(hass: HomeAssistant, entry):
    coordinator = entry.runtime_data
    await coordinator.unload()
    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    entry.runtime_data = None
    return True


def _register_coordinator_service(hass: HomeAssistant, name: str, handler):
    async def handler_(call):
        for entry_id in await service.async_extract_config_entry_ids(hass, call):
            if entry := hass.config_entries.async_get_entry(entry_id):
                if entry.domain == DOMAIN:
                    _LOGGER.debug(f"service call: {name} for {entry.domain}")
                    handler(entry.runtime_data, call.data)

    hass.services.async_register(DOMAIN, name, handler_)


async def async_setup(hass: HomeAssistant, config) -> bool:
    _register_coordinator_service(
        hass,
        "bridge_reboot",
        lambda coord, _: hass.async_create_task(coord.do_reboot()),
    )
    _register_coordinator_service(
        hass,
        "bridge_fwupdate",
        lambda coord, _: hass.async_create_task(coord.do_fwupdate()),
    )
    _register_coordinator_service(
        hass,
        "bridge_delete_callback",
        lambda coord, data: hass.async_create_task(
            coord.do_delete_callback(data.get("callback"))
        ),
    )
    _register_coordinator_service(
        hass,
        "execute_action",
        lambda coord, data: hass.async_create_task(
            coord.action_for_devices(data.get("action"))
        ),
    )

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
        return self.coordinator.model(self.device_id)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.device_id)},
            "name": self.get_name,
            "manufacturer": "Nuki",
            "model": self.model,
            "sw_version": self.data.get("firmwareVersion"),
        }


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
        return self.coordinator.bridge_info()

    @property
    def get_id(self):
        return self.coordinator.bridge_id()

    @property
    def device_info(self):
        """Correct HA-compliant Bridge info."""
        model = (
            "Hardware Bridge"
            if self.data.get("bridgeType", 1) == 1
            else "Software Bridge"
        )
        versions = self.data.get("versions") or {}

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

