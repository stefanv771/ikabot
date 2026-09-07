#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import json
import getpass
from collections import deque
from typing import Optional, Dict, Any

import requests
from urllib3.exceptions import InsecureRequestWarning

from ikabot import config
from ikabot.config import *
from ikabot.helpers.logging import getLogger
from ikabot.helpers.aesCipher import AESCipher
from ikabot.helpers.botComm import *
from ikabot.helpers.gui import banner
from ikabot.helpers.pedirInfo import read
from ikabot.helpers.varios import getDateTime, lastloginTimetoString
from ikabot.helpers.apiComm import getNewBlackBoxToken
from ikabot.helpers.action_lock import GlobalActionLock
from ikabot.web.lobby_client import LobbyClient
from ikabot.web.world_client import WorldClient

# Safe fallback for Globalpause if absent in this branch
try:
    from ikabot.helpers.Globalpause import wait_if_globally_paused
except ImportError:
    try:
        from ikabot.helpers.globalPause import wait_if_globally_paused
    except ImportError:
        def wait_if_globally_paused(username=None):
            pass

# Disable SSL verification warnings if configured
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)


def normal_get(url, params=None):
    """Sends a standard get request to the provided url (required by constructionList etc.)."""
    try:
        return requests.get(url, params=params or {})
    except requests.exceptions.ConnectionError:
        sys.exit("Internet connection failed")


class Session:
    """
    Modernized Session coordinator bridging Ikabot's feature modules
    with LobbyClient and WorldClient.
    """

    def __init__(self):
        self.padre = True
        self.logged = False
        self.logger = getLogger(__name__)
        self.requestHistory = deque(maxlen=5)

        # Standard browser fingerprint context (required by getNewBlackBoxToken)
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        self.api_user_agent = self.user_agent
        self.locale = getattr(config, "IKABOT_LOCALE", "en-GB").replace("_", "-")
        self.gf_lang = getattr(config, "IKABOT_GF_LANG", self.locale.split("-")[0])
        self.timezone_id = getattr(config, "IKABOT_TIMEZONE_ID", "Europe/London")

        if hasattr(config, "build_accept_language"):
            self.accept_language = config.build_accept_language(self.locale, self.gf_lang)
        else:
            self.accept_language = f"{self.gf_lang}-{self.gf_lang.upper()},{self.gf_lang};q=0.9,en;q=0.8"

        self.lobby_client: Optional[LobbyClient] = None
        self.world_client: Optional[WorldClient] = None
        self.action_lock: Optional[GlobalActionLock] = None
        
        self.__login()

    def setStatus(self, message: str):
        """Updates the status message displayed in the main menu process table."""
        self.logger.info(f"Changing status to {message}")
        sessionData = self.getSessionData()
        fileList = sessionData.get("processList", [])
        for p in fileList:
            if p.get("pid") == os.getpid():
                p["status"] = message
        sessionData["processList"] = fileList
        self.setSessionData(sessionData)

    def _obtain_blackbox(self) -> Optional[str]:
        """Generates a blackbox token with explicit console feedback."""
        try:
            print("[*] Generating Gameforge blackbox token...")
            token = getNewBlackBoxToken(self)
            print("[+] Blackbox token acquired.")
            return token
        except Exception as e:
            print(f"[!] Warning: Blackbox generation error: {e}")
            self.logger.warning(f"Failed to generate blackbox token: {e}")
            return None

    def __login(self, retries=0):
        if not self.logged:
            banner()
            self.mail = read(msg="Mail:")
            if len(config.predetermined_input) != 0:
                self.password = config.predetermined_input.pop(0)
            else:
                self.password = getpass.getpass("Password:")
            banner()

        self.cipher = AESCipher(self.mail, self.password)

        # 1. Initialize Lobby Client
        self.lobby_client = LobbyClient(email=self.mail, password=self.password, locale=self.locale)

        # Check for cached Lobby token (shared globally among all world accounts)
        globalSessionData = self.getSessionData()
        shared_lobby = globalSessionData.get("shared", {}).get("lobby", {})
        cached_gf_token = shared_lobby.get("gf-token-production")
        
        blackbox = None
        if cached_gf_token and self.lobby_client.authenticate_with_cookie(cached_gf_token):
            print("[+] Reusing active Gameforge Lobby session.")
        else:
            blackbox = self._obtain_blackbox()
            if not self.lobby_client.authenticate(blackbox=blackbox):
                sys.exit("Failed to authenticate with Gameforge Lobby.")

            # Save fresh lobby token to encrypted storage
            lobby_data = {"lobby": {"gf-token-production": self.lobby_client.token}}
            self.setSessionData(lobby_data, shared=True)

        # 2. Fetch Accounts & Servers from Lobby
        accounts = self.lobby_client.get_accounts()
        servers = self.lobby_client.get_servers()

        active_accounts = [a for a in accounts if not a.get("blocked", False)]
        if len(active_accounts) == 1:
            self.account = active_accounts[0]
        else:
            print("With which account do you want to log in?\n")
            for i, acc in enumerate(active_accounts):
                srv = acc["server"]
                srv_match = [s for s in servers if s.get("accountGroup") == acc.get("accountGroup")]
                world_name = srv_match[0]["name"] if srv_match else "Unknown"
                print(f"({i + 1}) {acc['name']} [s{srv['number']}-{srv['language']} - {world_name}]")
            num = read(min=1, max=len(active_accounts))
            self.account = active_accounts[num - 1]

        # Bind world coordinates to session
        self.username = self.account["name"]
        self.login_servidor = self.account["server"]["language"]
        self.mundo = str(self.account["server"]["number"])
        self.servidor = self.login_servidor
        
        # Initialize Server-Scoped Action Lock for this specific game world
        self.action_lock = GlobalActionLock(
            username=self.username,
            server=f"s{self.mundo}-{self.servidor}",
            min_delay_seconds=15
        )

        # 3. Initialize World Client
        self.world_client = WorldClient(serv_number=int(self.mundo), serv_lang=self.servidor)
        self.host = self.world_client.host
        self.urlBase = self.world_client.url_base
        self.s = self.world_client.session

        # 4. Check for cached world cookies partitioned by account ID and server
        account_storage_key = f"cookies_{self.account['id']}_{self.mundo}_{self.servidor}"
        accountSessionData = self.getSessionData()
        cached_cookies = accountSessionData.get(account_storage_key, {})
        used_cached_cookie = False

        if "ikariam" in cached_cookies:
            print(f"[*] Testing cached world session for {self.username} on s{self.mundo}-{self.servidor}...")
            for name, val in cached_cookies.items():
                cookie_obj = requests.cookies.create_cookie(
                    domain=self.host,
                    name=name,
                    value=val
                )
                self.s.cookies.set_cookie(cookie_obj)

            try:
                test_html = self.world_client.get_city_view()
                if not self.world_client.is_expired(test_html):
                    print(f"[+] CACHE HIT: Reusing active world session for {self.username}. (Zero logins needed!)")
                    used_cached_cookie = True
            except Exception as e:
                print(f"[-] Cached world session check failed: {e}")

        # 5. Automated SSO Login Redirect (Only if cache was empty or expired)
        if not used_cached_cookie:
            print(f"[*] Generating new SSO session redirect for {self.username}...")
            if not blackbox:
                blackbox = self._obtain_blackbox()

            login_url = None
            if blackbox:
                try:
                    login_url = self.lobby_client.get_login_link(
                        account_id=self.account["id"],
                        serv_lang=self.servidor,
                        serv_number=int(self.mundo),
                        blackbox=blackbox
                    )
                except Exception as e:
                    print(f"[!] get_login_link error: {e}")

            if login_url and self.world_client.login_via_url(login_url):
                print(f"[+] Automated SSO login succeeded for {self.username}!")
            else:
                print("\nAutomated world link failed. Manual cookie fallback:")
                print(f"Open browser -> https://{self.host} -> DevTools (F12) -> Cookies -> 'ikariam'")
                manual_cookie = read(msg="Enter 'ikariam' cookie manually: ").strip()
                self.world_client.set_session_cookie(manual_cookie)

            # Persist the freshly acquired cookies partitioned by account ID
            accountSessionData[account_storage_key] = dict(self.s.cookies.items())
            self.setSessionData(accountSessionData)
            print(f"[+] Saved fresh session cookies for {self.username}.")

        config.infoUser = f"Server:{self.servidor}, World:{self.mundo}, Player:{self.username}"
        banner()
        self.logged = True

    def isExpired(self, html):
        """Checks if the game session has expired."""
        return self.world_client.is_expired(html)

    def get(self, url="", params=None, ignoreExpire=False, noIndex=False, fullResponse=False, noQuery=False, **kwargs):
        """Sends a GET request to the game server."""
        wait_if_globally_paused(getattr(self, "username", None))
        
        base_url = self.urlBase.replace("index.php", "") if noIndex else self.urlBase
        full_url = (base_url + url).replace("?", "") if noQuery else (base_url + url)

        response = self.s.get(full_url, params=params or {}, verify=config.do_ssl_verify, timeout=300, **kwargs)
        
        if not ignoreExpire and self.world_client.is_expired(response.text):
            self.logger.warning("Session expired detected in GET request. Re-logging...")
            self.__login()
            return self.get(url=url, params=params, ignoreExpire=ignoreExpire, noIndex=noIndex, fullResponse=fullResponse, noQuery=noQuery, **kwargs)

        return response if fullResponse else response.text

    def post(self, url="", payloadPost=None, params=None, ignoreExpire=False, noIndex=False, fullResponse=False, noQuery=False, **kwargs):
        """Sends a POST request to the game server with automatic CSRF actionRequest injection and action queueing."""
        wait_if_globally_paused(getattr(self, "username", None))

        # Enforce server-scoped multi-account human cooldown
        if getattr(self, "action_lock", None):
            self.action_lock.wait_turn()

        payloadPost = payloadPost or {}
        params = params or {}

        # Ensure active CSRF token
        html = self.get(ignoreExpire=True)
        token = self.world_client.extract_action_request(html) or ""
        
        if "actionRequest" in payloadPost:
            payloadPost["actionRequest"] = token
        if "actionRequest" in params:
            params["actionRequest"] = token
        url = url.replace(actionRequest, token)

        base_url = self.urlBase.replace("index.php", "") if noIndex else self.urlBase
        full_url = (base_url + url).replace("?", "") if noQuery else (base_url + url)

        response = self.s.post(full_url, data=payloadPost, params=params, verify=config.do_ssl_verify, timeout=300, **kwargs)

        if not ignoreExpire and self.world_client.is_expired(response.text):
            self.logger.warning("Session expired detected in POST request. Re-logging...")
            self.__login()
            return self.post(url=url, payloadPost=payloadPost, params=params, ignoreExpire=ignoreExpire, noIndex=noIndex, fullResponse=fullResponse, noQuery=noQuery, **kwargs)

        return response if fullResponse else response.text

    def logout(self):
        """Kills the current process cleanly."""
        self.logger.info("logout()")
        if not self.padre:
            sys.exit(0)

    def setSessionData(self, sessionData, shared=False):
        """Encrypts and persists session data."""
        self.cipher.setSessionData(self, sessionData, shared=shared)

    def getSessionData(self):
        """Retrieves encrypted session data."""
        return self.cipher.getSessionData(self)