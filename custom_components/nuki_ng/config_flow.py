from homeassistant import config_entries
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.network import get_url
from .constants import DOMAIN
from .nuki import NukiInterface

import logging
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)


class NukiNGConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    async def async_step_reauth(self, user_input):
        return await self.async_step_user(user_input)

    async def find_nuki_devices(self, config: dict):
        nuki = NukiInterface(
            self.hass,
            bridge=config.get("address"),
            token=config.get("token"),
            web_token=config.get("web_token")
        )
        title = None
        use_hashed_token = False
        if nuki.token:
            hass_url = config.get("hass_url", "")
            if hass_url.lower().startswith("https://"):
                _LOGGER.error(
                    f"Bridge doesn't support HTTPS callback URLs: {hass_url}")
                return title, "https_not_supported", None
            try:
                info = await nuki.bridge_info()
                use_hashed_token = info.get("bridgeType") == 1
                response = await nuki.bridge_list()
                _LOGGER.debug(f"bridge devices: {response}")
                first_device = next(iter(response.values()), {})
                title = first_device.get("name")
                if not title:
                    return title, "invalid_bridge_token", None
            except Exception as err:
                _LOGGER.exception(
                    f"Failed to get list of devices from bridge: {err}")
                return title, "invalid_bridge_token", None
        if nuki.web_token:
            try:
                response = await nuki.web_list()
                _LOGGER.debug(f"web devices: {response}")
                if not title:
                    first_device = next(iter(response.values()), {})
                    title = first_device.get("name")
            except Exception as err:
                _LOGGER.exception(
                    f"Failed to get list of devices from web API: {err}")
                return title, "invalid_web_token", None
        return title, None, use_hashed_token

    def _get_hass_url(self, hass):
        try:
            return get_url(hass)
        except Exception as err:
            _LOGGER.exception(f"Error getting HASS url: {err}")
            return ""

    def _schema(self, user_input: dict):
        return vol.Schema({
            vol.Optional("address", default=user_input.get("address", "")): cv.string,
            vol.Optional("hass_url", default=user_input.get("hass_url", "")): cv.string,
            vol.Optional("token", default=user_input.get("token", "")): cv.string,
            vol.Optional("web_token", default=user_input.get("web_token", "")): cv.string,
            vol.Optional("name", default=user_input.get("name", "")): cv.string,
            vol.Required("update_seconds", default=user_input.get("update_seconds", 30)): vol.All(
                cv.positive_int,
                vol.Range(min=10, max=600)
            ),
        })

    async def _validate(self, user_input: dict):
        if not user_input.get("token") and not user_input.get("web_token"):
            return "no_token", None
        if user_input.get("token") and not user_input.get("address"):
            return "no_bridge_url", None
        title, err, use_hashed_token = await self.find_nuki_devices(user_input)
        if err:
            return err, None
        return None, {**user_input, "use_hashed": use_hashed_token, "name": user_input.get("name") or title}

    async def async_step_user(self, user_input):
        errors = None
        _LOGGER.debug(f"Input: {user_input}")
        if user_input is None:
            nuki = NukiInterface(self.hass)
            bridge_address = await nuki.discover_bridge()
            hass_url = self._get_hass_url(self.hass)
            user_input = {
                "address": bridge_address,
                "hass_url": hass_url
            }
        else:
            err, data = await self._validate(user_input)
            if not err:
                return self.async_create_entry(title=data["name"], data=data)
            errors = dict(base=err)
        return self.async_show_form(
            step_id="user", data_schema=self._schema(user_input), errors=errors
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = None
        if user_input is None:
            user_input = dict(entry.data)
        else:
            err, data = await self._validate(user_input)
            if not err:
                return self.async_update_reload_and_abort(entry, data_updates=data)
            errors = dict(base=err)
        return self.async_show_form(
            step_id="reconfigure", data_schema=self._schema(user_input), errors=errors
        )

    # def async_get_options_flow(config_entry):
    #     return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        data = self.config_entry.as_dict()["data"]
        _LOGGER.debug(f"OptionsFlowHandler: {data} {self.config_entry}")
        schema = vol.Schema({
            vol.Required("hass_url", default=data.get("hass_url")): cv.string,
            vol.Required("token", default=data.get("token")): cv.string,
            vol.Optional("web_token", default=data.get("web_token")): cv.string,
        })
        return self.async_show_form(
            step_id="options", data_schema=schema
        )
