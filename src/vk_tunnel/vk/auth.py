"""
VK authentication — session management via vk_api.

Handles login with phone + password, 2FA, and token-based auth.
Persists session to avoid re-login on restarts.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog
import vk_api

log = structlog.get_logger()

SESSION_FILE = Path.home() / ".vk-tunnel-session.json"


class VKAuth:
    """Manage VK API session."""

    def __init__(
        self,
        login: str = "",
        password: str = "",
        token: str = "",
        session_file: Path = SESSION_FILE,
    ):
        self._login = login
        self._password = password
        self._token = token
        self._session_file = session_file
        self._vk_session: vk_api.VkApi | None = None
        self._api = None

    def auth(self) -> vk_api.VkApi:
        """Authenticate and return VkApi session."""
        if self._token:
            self._vk_session = vk_api.VkApi(token=self._token)
            log.info("vk auth: using token")
        elif self._login and self._password:
            self._vk_session = vk_api.VkApi(
                login=self._login,
                password=self._password,
                auth_handler=self._two_factor_handler,
                config_filename=str(self._session_file),
            )
            self._vk_session.auth(token_only=True)
            log.info("vk auth: logged in", login=self._login)
        else:
            raise ValueError("Provide either VK token or login+password")

        self._api = self._vk_session.get_api()
        return self._vk_session

    @property
    def api(self):
        """VK API accessor."""
        if not self._api:
            self.auth()
        return self._api

    @property
    def session(self) -> vk_api.VkApi:
        if not self._vk_session:
            self.auth()
        return self._vk_session

    @staticmethod
    def _two_factor_handler() -> tuple[str, bool]:
        """2FA handler — prompts user for code."""
        code = input("Enter VK 2FA code: ")
        remember = True
        return code, remember
