import asyncio
import json
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff schedule: retry quickly at first, then back off,
# capping out so we're not hammering a genuinely offline/rebooting PDU.
RECONNECT_DELAYS = [2, 5, 10, 20, 30, 60]


class CrestronPduClient:
    """
    Client for a Crestron PC-350V series PDU.

    Includes automatic reconnection: if the WebSocket connection drops for
    any reason (PDU reboot, network blip, HA restart timing, etc.), this
    client re-authenticates and reconnects on a backoff schedule rather
    than requiring a manual integration reload. Any entity that registered
    as a listener is notified of connect/disconnect so it can reflect
    accurate availability instead of silently going stale.
    """

    def __init__(self, host: str, username: str, password: str):
        self._host = host
        self._username = username
        self._password = password
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._connection_task: asyncio.Task | None = None
        self._stopping = False

        self._state_listeners: list = []          # called with pushed state dicts
        self._availability_listeners: list = []    # called with True/False on connect/disconnect

        self.is_connected = False

    # ---------- listener registration ----------

    def add_state_listener(self, callback):
        """Register a callback(partial_state: dict) for pushed state updates."""
        self._state_listeners.append(callback)

    def remove_state_listener(self, callback):
        if callback in self._state_listeners:
            self._state_listeners.remove(callback)

    def add_availability_listener(self, callback):
        """Register a callback(is_connected: bool) for connect/disconnect events."""
        self._availability_listeners.append(callback)

    def remove_availability_listener(self, callback):
        if callback in self._availability_listeners:
            self._availability_listeners.remove(callback)

    def _notify_availability(self, is_connected: bool):
        self.is_connected = is_connected
        for cb in self._availability_listeners:
            try:
                cb(is_connected)
            except Exception:
                _LOGGER.exception("Error in PDU availability listener")

    # ---------- login ----------

    async def async_login(self) -> bool:
        """
        Log in and establish a session.

        Uses a single aiohttp ClientSession with an unsafe cookie jar (the
        PDU is addressed by bare IP, and aiohttp's default jar silently
        drops cookies for non-domain hosts unless told not to).
        """
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(cookie_jar=jar)

        try:
            login_page_url = f"https://{self._host}/userlogin.html"

            # Step 1: GET the login page first to pick up any session cookie
            # (TRACKID) the server expects to see already present on the POST.
            async with self._session.get(login_page_url, ssl=False) as resp:
                _LOGGER.debug("PDU login page GET status: %s", resp.status)

            # Step 2: POST credentials with the headers the server requires
            # (Origin/Referer - without these it returns 403).
            data = {"login": self._username, "passwd": self._password}
            headers = {
                "X-Requested-With": "XMLHttpRequest",
                "Origin": f"https://{self._host}",
                "Referer": login_page_url,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }

            async with self._session.post(
                login_page_url, data=data, headers=headers, ssl=False
            ) as resp:
                body_preview = (await resp.text())[:500]
                if resp.status != 200:
                    _LOGGER.error(
                        "PDU login failed - status %s, body: %s", resp.status, body_preview
                    )
                    return False

                cookie_names = {c.key for c in self._session.cookie_jar}
                if "AuthByPasswd" not in cookie_names:
                    _LOGGER.error(
                        "PDU login returned 200 but no session cookie was set "
                        "(likely wrong credentials). Body preview: %s", body_preview
                    )
                    return False

                return True
        except aiohttp.ClientError as err:
            _LOGGER.error("PDU login request failed: %s", err)
            return False

    # ---------- state ----------

    async def async_get_full_state(self) -> dict:
        """GET the entire device tree (used at startup and after reconnects)."""
        url = f"https://{self._host}/Device"
        async with self._session.get(url, ssl=False) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    # ---------- connection management ----------

    async def async_start(self):
        """
        Start the managed connection: logs in, connects the WebSocket, and
        keeps a background task running that automatically re-logs-in and
        reconnects on disconnect, with backoff. Call this once; it manages
        its own lifetime until async_close() is called.
        """
        self._stopping = False
        self._connection_task = asyncio.create_task(self._connection_loop())

        # Wait briefly for the first connection attempt to resolve, so
        # callers can know immediately whether startup succeeded, without
        # blocking forever if the PDU is genuinely unreachable.
        for _ in range(50):  # up to ~5s
            if self.is_connected:
                return True
            await asyncio.sleep(0.1)
        return self.is_connected

    async def _connection_loop(self):
        """Background task: connect, listen, and reconnect on failure indefinitely."""
        attempt = 0
        while not self._stopping:
            try:
                if not await self.async_login():
                    raise RuntimeError("login failed")

                url = f"wss://{self._host}/websockify"
                headers = {"Origin": f"https://{self._host}"}
                self._ws = await self._session.ws_connect(
                    url, ssl=False, heartbeat=30, headers=headers
                )

                attempt = 0  # reset backoff after a successful connect
                self._notify_availability(True)
                _LOGGER.info("Connected to PDU WebSocket at %s", url)

                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        if "Device" in data:
                            for cb in self._state_listeners:
                                try:
                                    cb(data["Device"])
                                except Exception:
                                    _LOGGER.exception("Error in PDU state listener")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("PDU connection to %s lost/failed: %s", self._host, err)

            # We only reach here after a disconnect or failed attempt.
            self._notify_availability(False)

            if self._stopping:
                break

            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            _LOGGER.info("PDU %s disconnected, retrying in %ss", self._host, delay)
            await asyncio.sleep(delay)

    # ---------- commands ----------

    async def async_set_outlet(self, outlet_id: str, is_on: bool):
        """Turn an outlet on/off. outlet_id must be zero-padded, e.g. '01', '17'."""
        if not self.is_connected or self._ws is None or self._ws.closed:
            raise RuntimeError("PDU WebSocket is not connected")

        command = {
            "Device": {"PowerController": {"Outlets": {outlet_id: {"IsOn": is_on}}}}
        }
        await self._ws.send_str(json.dumps(command))

    async def async_cycle_outlet(self, outlet_id: str):
        """Power cycle a single outlet. outlet_id must be zero-padded."""
        if not self.is_connected or self._ws is None or self._ws.closed:
            raise RuntimeError("PDU WebSocket is not connected")

        command = {
            "Device": {"PowerController": {"Outlets": {outlet_id: {"CycleOutlet": True}}}}
        }
        await self._ws.send_str(json.dumps(command))

    async def async_reset_voltage_protection(self):
        """
        Reset the PDU's voltage protection trip (also observed to clear
        over-current trips in practice, per real-world use, even though
        the field name only mentions Voltage). Confirmed against real
        hardware via captured WebSocket traffic.

        NOTE: per direct experience with this device, outlets remain
        unusable after this reset until the PDU is rebooted - see
        async_reboot() / async_reset_and_reboot() below.
        """
        if not self.is_connected or self._ws is None or self._ws.closed:
            raise RuntimeError("PDU WebSocket is not connected")

        command = {
            "Device": {
                "PowerController": {
                    "Protection": {
                        "Voltage": {"ResetVoltageProtection": True}
                    }
                }
            }
        }
        await self._ws.send_str(json.dumps(command))

    async def async_reboot(self):
        """
        Reboot the entire PDU. Confirmed against real hardware via captured
        WebSocket traffic - the real Crestron web UI reported ~210 seconds
        (3.5 minutes) for the device to fully come back online.

        The WebSocket connection will drop immediately after this command
        is sent (the device is rebooting). This client's automatic
        reconnect logic will keep retrying on its backoff schedule and
        reconnect on its own once the PDU is back up - no manual action
        needed in Home Assistant.
        """
        if not self.is_connected or self._ws is None or self._ws.closed:
            raise RuntimeError("PDU WebSocket is not connected")

        command = {"Device": {"DeviceOperations": {"Reboot": True}}}
        await self._ws.send_str(json.dumps(command))

    async def async_reset_and_reboot(self):
        """
        Convenience method matching real-world usage of this device:
        resetting voltage/over-current protection alone does NOT restore
        outlet function - a reboot is required afterward. This sends both
        in sequence, matching the actual fix procedure.
        """
        await self.async_reset_voltage_protection()
        # Small delay so the reset command is processed before the reboot
        # drops the connection - not strictly confirmed necessary, but
        # cheap insurance against a race between the two commands.
        await asyncio.sleep(1.0)
        await self.async_reboot()

    # ---------- shutdown ----------

    async def async_close(self):
        """Clean shutdown - stop the reconnect loop and close the WebSocket/session."""
        self._stopping = True
        if self._connection_task:
            self._connection_task.cancel()
            try:
                await self._connection_task
            except asyncio.CancelledError:
                pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
