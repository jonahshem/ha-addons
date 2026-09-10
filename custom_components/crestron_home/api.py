import aiohttp
import logging
from aiohttp import TCPConnector

_LOGGER = logging.getLogger(__name__)


class CrestronHomeAPI:
    """
    Client for the Crestron Home CWS REST API.

    Holds one persistent aiohttp session for the life of the integration
    instead of creating a new session (and new TLS handshake) on every
    single call - the previous version opened/closed a session per request,
    which is wasteful given this gets polled every few seconds.
    """

    def __init__(self, host, token):
        self.host = host
        self.token = token
        self.auth_key = None
        self.base_url = f"https://{host}:443/cws/api"
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared session, creating it on first use."""
        if self._session is None or self._session.closed:
            connector = TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def login(self):
        """Handshake to get the session authkey."""
        url = f"{self.base_url}/login"
        headers = {"Crestron-RestAPI-AuthToken": self.token}

        session = self._get_session()
        try:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Verified lowercase 'authkey' from your curl test
                    self.auth_key = data.get("authkey") or data.get("AuthKey")
                    if self.auth_key:
                        _LOGGER.info("Successfully logged into Crestron Home")
                        return True
                _LOGGER.error("Crestron login failed with status: %s", resp.status)
                return False
        except Exception as e:
            _LOGGER.error("Connection error during login: %s", e)
            return False

    async def request(self, method, endpoint, payload=None):
        """Make an authenticated request to the processor, reusing the shared session."""
        if not self.auth_key:
            if not await self.login():
                return None

        url = f"{self.base_url}{endpoint}"
        headers = {
            "Crestron-RestAPI-AuthKey": str(self.auth_key),
            "Content-Type": "application/json"
        }

        session = self._get_session()
        try:
            async with session.request(
                method,
                url,
                json=payload,
                headers=headers,
                timeout=15
            ) as resp:
                if resp.status == 401:
                    # Session expired server-side - clear the key so the
                    # next call re-authenticates. Note we do NOT retry the
                    # original request automatically here, to avoid
                    # infinite loops on a genuinely broken auth token.
                    self.auth_key = None
                    return None
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            _LOGGER.error("API Request error: %s", e)
            return None

    async def async_close(self):
        """Close the shared session. Call this on integration unload."""
        if self._session and not self._session.closed:
            await self._session.close()
