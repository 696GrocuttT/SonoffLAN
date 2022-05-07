import asyncio
import logging
from typing import Dict, Union

from homeassistant.components.remote import RemoteEntity, ATTR_DELAY_SECS, \
    DEFAULT_DELAY_SECS
from homeassistant.const import ATTR_COMMAND

from .binary_sensor import XRemoteSensor, XRemoteSensorOff
from .button import XRemoteButton
from .core.const import DOMAIN
from .core.entity import XEntity
from .core.ewelink import XRegistry, SIGNAL_ADD_ENTITIES

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0  # fix entity_platform parallel_updates Semaphore


async def async_setup_entry(hass, config_entry, add_entities):
    ewelink: XRegistry = hass.data[DOMAIN][config_entry.entry_id]
    ewelink.dispatcher_connect(
        SIGNAL_ADD_ENTITIES,
        lambda x: add_entities([e for e in x if isinstance(e, RemoteEntity)])
    )


def rfbridge_childs(remotes: list):
    try:
        yaml = XRegistry.config["rfbridge"]
    except Exception:
        yaml = {}

    childs = {}
    # For dual RF sensors: {payload_on channel: payload_off name}
    duals = {}

    for remote in remotes:
        for button in remote["buttonName"]:
            channel = next(iter(button))

            # remote_type 6 (alarm) has one button without name
            if remote["remote_type"] != "6":
                child = {"name": button[channel], "device_class": "button"}
            else:
                child = {"name": remote["name"]}

            # everride child params from YAML
            if child["name"] in yaml:
                child.update(yaml[child["name"]])

                if "payload_off" in child:
                    duals[channel] = child["payload_off"]

                # child with timeout or payload_off can't be a button
                if child.get("device_class") == "button" and (
                        "payload_off" in child or "timeout" in child
                ):
                    child.pop("device_class")

            child["channel"] = channel
            childs[channel] = child

    for ch, name in duals.items():
        ch_off = next(k for k, v in childs.items() if v["name"] == name)
        # move off channel to end of the dict
        childs[ch_off] = childs.pop(ch_off)
        childs[ch_off]["channel_on"] = ch

    return childs


# noinspection PyAbstractClass
class XRemote(XEntity, RemoteEntity):
    _attr_is_on = True
    childs: Dict[
        str, Union[XRemoteButton, XRemoteSensor, XRemoteSensorOff]
    ] = None

    def __init__(self, ewelink: XRegistry, device: dict):
        try:
            # only learned channels
            channels = [str(c["rfChl"]) for c in device["params"]["rfList"]]

            childs = rfbridge_childs(device["tags"]["zyx_info"])
            for ch, child in childs.items():
                if ch not in channels:
                    continue

                if "channel_on" in child:
                    sensor = childs[child["channel_on"]]
                    childs[ch] = XRemoteSensorOff(child, sensor)
                elif child.get("device_class") == "button":
                    childs[ch] = XRemoteButton(ewelink, device, child)
                else:
                    childs[ch] = XRemoteSensor(ewelink, device, child)
            ewelink.dispatcher_send(SIGNAL_ADD_ENTITIES, childs.values())
            self.childs = childs

        except Exception as e:
            _LOGGER.error(
                f"{self.unique_id} | can't setup RFBridge", exc_info=e
            )

        # init bridge after childs for update available
        XEntity.__init__(self, ewelink, device)

        self.params = {"cmd", "arming"}
        self.ts = None

    def set_state(self, params: dict):
        # skip full cloud state update
        if not self.is_on or "init" in params:
            return

        for param, ts in params.items():
            if not param.startswith("rfTrig"):
                continue

            # skip first msg from LAN because it sent old trigger event with
            # local discovery and only LAN sends arming param
            if self.ts is None and params.get("arming"):
                self.ts = ts
                return

            # skip same cmd from local and cloud
            if ts == self.ts:
                return

            self.ts = ts

            child = self.childs.get(param[6:])
            if not child:
                return
            child.internal_update(ts)

            self._attr_extra_state_attributes = data = {
                "command": int(child.channel), "name": child.name,
                "entity_id": self.entity_id, "ts": ts,
            }
            self.hass.bus.async_fire("sonoff.remote", data)

    async def async_send_command(self, command, **kwargs):
        delay = kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS)
        for i, channel in enumerate(command):
            if i:
                await asyncio.sleep(delay)

            # transform button name to channel number
            if not channel.isdigit():
                channel = next(
                    k for k, v in self.childs.items() if v.name == channel
                )

            # cmd param for local and for cloud mode
            await self.ewelink.send(self.device, {
                "cmd": "transmit", "rfChl": int(channel)
            })

    async def async_learn_command(self, **kwargs):
        command = kwargs[ATTR_COMMAND]
        # cmd param for local and for cloud mode
        await self.ewelink.send(self.device, {
            "cmd": "capture", "rfChl": int(command[0])
        })

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self._async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self._async_write_ha_state()
