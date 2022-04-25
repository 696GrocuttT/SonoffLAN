import asyncio
import logging
import time
from typing import Dict, List

from aiohttp import ClientSession

from .base import XRegistryBase, SIGNAL_UPDATE, SIGNAL_CONNECTED
from .cloud import XRegistryCloud
from .local import XRegistryLocal, decrypt

_LOGGER = logging.getLogger(__name__)

SIGNAL_ADD_ENTITIES = "add_entities"


class XRegistry(XRegistryBase):
    config: dict = None
    task: asyncio.Task = None

    def __init__(self, session: ClientSession):
        super().__init__(session)

        self.devices: Dict[str, dict] = {}

        self.cloud = XRegistryCloud(session)
        self.cloud.dispatcher_connect(SIGNAL_CONNECTED, self.cloud_connected)
        self.cloud.dispatcher_connect(SIGNAL_UPDATE, self.cloud_update)

        self.local = XRegistryLocal(session)
        self.local.dispatcher_connect(SIGNAL_UPDATE, self.local_update)

    def setup_devices(self, devices: List[dict]):
        from ..devices import get_spec

        for device in devices:
            deviceid = device["deviceid"]
            try:
                device.update(self.config["devices"][deviceid])
            except Exception:
                pass

            uiid = device['extra']['uiid']
            _LOGGER.debug(f"{deviceid} UIID {uiid:04} | %s", device["params"])

            spec = get_spec(device)
            entities = [cls(self, device) for cls in spec]
            self.dispatcher_send(SIGNAL_ADD_ENTITIES, entities)

            self.devices[deviceid] = device

    async def stop(self):
        self.devices.clear()
        self.dispatcher.clear()

        await self.cloud.stop()
        await self.local.stop()

        if self.task:
            self.task.cancel()

    async def send(
            self, device: dict, params: dict, params_lan: dict = None,
            query_cloud: bool = True
    ):
        """Send command to device with LAN and Cloud. Usual params are same.

        :param device: device object
        :param params: usual params are same
        :param params_lan: optional if LAN params different (ex iFan03)
        :param query_cloud: optional query Cloud device state after send cmd
        """
        seq = self.sequence()

        can_local = self.local.online and device.get('host')
        can_cloud = self.cloud.online and device.get('online')

        if can_local and can_cloud:
            # try to send a command locally (wait no more than a second)
            ok = await self.local.send(device, params_lan or params, seq, 1)

            # otherwise send a command through the cloud
            if ok != 'online':
                ok = await self.cloud.send(device, params, seq)
                if ok != 'online':
                    coro = self.local.check_offline(device)
                    asyncio.create_task(coro)
                elif query_cloud:
                    # force update device actual status
                    await self.cloud.send(device, timeout=0)

        elif can_local:
            ok = await self.local.send(device, params_lan or params, seq, 5)
            if ok != 'online':
                coro = self.local.check_offline(device)
                asyncio.create_task(coro)

        elif can_cloud:
            ok = await self.cloud.send(device, params, seq)
            if ok == "online" and query_cloud:
                await self.cloud.send(device, timeout=0)

        else:
            return

        # TODO: response state
        # self.dispatcher_send(device["deviceid"], state)

    def cloud_connected(self):
        for deviceid in self.devices.keys():
            self.dispatcher_send(deviceid)

        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.pow_helper())

    def cloud_update(self, msg: dict):
        did = msg["deviceid"]
        device = self.devices.get(did)
        if not device:
            _LOGGER.warning(f"UNKNOWN cloud device: {msg}")
            return

        params = msg["params"]

        _LOGGER.debug(f"{did} <= Cloud3 | %s | {msg.get('sequence')}", params)

        # process online change
        if "online" in params:
            # skip same online
            if device["online"] == params["online"]:
                return
            device["online"] = params["online"]

        # any message from device - set device online to True
        elif device["online"] is False:
            device["online"] = True

        self.dispatcher_send(did, params)

    def local_update(self, msg: dict):
        did: str = msg["deviceid"]
        device: dict = self.devices.get(did)
        params: dict = msg.get("params")
        if not device:
            if not params:
                try:
                    msg["params"] = params = self.local.decrypt_msg(
                        msg, self.config["devices"][did]["devicekey"]
                    )
                except Exception:
                    _LOGGER.debug(f"{did} !! skip setup for encrypted device")
                    self.devices[did] = msg
                    return

            from ..devices import setup_diy
            device = setup_diy(msg)
            self.setup_devices([device])

        elif not params:
            if "devicekey" not in device:
                return
            try:
                params = self.local.decrypt_msg(msg, device["devicekey"])
            except Exception as e:
                _LOGGER.debug("Can't decrypt message", exc_info=e)
                return

        _LOGGER.debug(f"{did} <= Local3 | %s | {msg.get('seq')}", params)

        if "online" in params:
            if params["online"] is None:
                coro = self.local.check_offline(device)
                asyncio.create_task(coro)
            elif params["online"] is False:
                self.dispatcher_send(msg["deviceid"])
            return

        device["host"] = msg.get("host")  # get for tests

        self.dispatcher_send(did, params)

    async def pow_helper(self):
        from ..devices import POW_UI_ACTIVE

        # collect pow devices
        devices = [
            device for device in self.devices.values()
            if "extra" in device and device["extra"]["uiid"] in POW_UI_ACTIVE
        ]
        if not devices:
            return

        while True:
            if not self.cloud.online:
                await asyncio.sleep(60)
                continue

            ts = time.time()

            for device in devices:
                if not device["online"] or device.get("pow_ts", 0) > ts:
                    continue

                dt, params = POW_UI_ACTIVE[device["extra"]["uiid"]]
                device["pow_ts"] = ts + dt
                await self.cloud.send(device, params, timeout=0)

            # sleep for 150 seconds (because minimal uiActive - 180 seconds)
            await asyncio.sleep(150)
