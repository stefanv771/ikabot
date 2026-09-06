# ikabot/web/lobby_client.py
import re
import json
import requests
from typing import List, Dict, Optional, Tuple


class LobbyClient:
    """Handles communication strictly with Gameforge Lobby and Spark authentication."""

    LOBBY_BASE = "https://lobby.ikariam.gameforge.com"
    SPARK_BASE = "https://spark-web.gameforge.com"

    def __init__(self, email: str, password: str, locale: str = "en-GB"):
        self.email = email
        self.password = password
        # Ensure locale uses a hyphen (e.g. 'en-GB') for Spark API validation
        self.locale = locale.replace("_", "-")
        self.gf_lang = self.locale.split("-")[0]
        
        self.session = requests.Session()
        self.token: Optional[str] = None
        
        self._setup_headers()
    def _setup_headers(self):
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": f"{self.gf_lang}-{self.gf_lang.upper()},{self.gf_lang};q=0.9,en;q=0.8",
            "Connection": "keep-alive"
        })

    def _get_game_environment_ids(self) -> Tuple[str, str]:
        """Fetches dynamic platformGameId and gameEnvironmentId from Gameforge config."""
        url = f"{self.LOBBY_BASE}/config/configuration.js"
        resp = self.session.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch Lobby configuration: HTTP {resp.status_code}")
            
        env_match = re.search(r'"gameEnvironmentId":"(.*?)"', resp.text)
        game_match = re.search(r'"platformGameId":"(.*?)"', resp.text)
        
        if not env_match or not game_match:
            raise ValueError("Could not extract gameEnvironmentId or platformGameId from config.")
            
        return env_match.group(1), game_match.group(1)

    def set_token(self, token: str):
        """Stores the token in memory and sets the gf-token-production cookie."""
        clean_token = token.strip().strip('"').strip("'")
        if clean_token.lower().startswith("bearer "):
            clean_token = clean_token[7:].strip()
            
        self.token = clean_token
        cookie = requests.cookies.create_cookie(
            domain=".gameforge.com",
            name="gf-token-production",
            value=self.token,
        )
        self.session.cookies.set_cookie(cookie)

    def authenticate_with_cookie(self, token: str) -> bool:
        """Verifies if an existing token is still valid."""
        self.set_token(token)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Host": "lobby.ikariam.gameforge.com"
        }
        resp = self.session.get(f"{self.LOBBY_BASE}/api/users/me", headers=headers)
        return resp.status_code == 200

    def login_with_credentials(self, blackbox: str) -> bool:
        """
        Logs into Gameforge automatically using email, password, and blackbox.
        Sets self.token automatically on success.
        """
        env_id, game_id = self._get_game_environment_ids()

        headers = {
            "Accept": "*/*",
            "Origin": self.LOBBY_BASE,
            "Referer": f"{self.LOBBY_BASE}/",
            "Content-Type": "application/json",
            "User-Agent": self.session.headers["User-Agent"]
        }

        payload = {
            "identity": self.email,
            "password": self.password,
            "locale": self.locale,
            "gfLang": self.gf_lang,
            "gameId": game_id,
            "gameEnvironmentId": env_id,
            "blackbox": blackbox,
        }

        url = f"{self.SPARK_BASE}/api/v2/authProviders/mauth/sessions"
        resp = self.session.post(url, json=payload, headers=headers)

        # Handle Two-Factor Authentication (2FA) if enabled
        if resp.status_code == 409 and "OTP_REQUIRED" in resp.text:
            otp_code = input("\n[2FA] Enter your 6-digit two-factor authentication code: ").strip()
            payload["otpCode"] = otp_code
            resp = self.session.post(url, json=payload, headers=headers)

        if resp.status_code not in (200, 201):
            print(f"[-] Automated login failed (HTTP {resp.status_code}): {resp.text}")
            return False

        data = resp.json()
        if "token" in data:
            self.set_token(data["token"])
            return True

        return False

    def authenticate(self, blackbox: Optional[str] = None, cached_token: Optional[str] = None) -> bool:
        """
        High-level authentication flow:
        1. Try cached token if provided.
        2. Try automated login using email + password + blackbox.
        3. Fallback to manual token entry only if automated login fails.
        """
        # 1. Try cached token
        if cached_token and self.authenticate_with_cookie(cached_token):
            print("[+] Logged in using cached lobby session.")
            return True

        # 2. Try automated credential login
        if blackbox:
            print("[*] Attempting automated Gameforge login with credentials...")
            if self.login_with_credentials(blackbox=blackbox):
                print("[+] Automated Gameforge login succeeded! Token acquired.")
                return True

        # 3. Fallback
        print("\n[-] Automated login could not proceed (e.g. captcha or rejected blackbox).")
        manual_token = input("Paste your 'gf-token-production' cookie manually: ").strip()
        return self.authenticate_with_cookie(manual_token)

    def get_accounts(self) -> List[Dict]:
        """Fetches all Ikariam game accounts associated with this Gameforge user."""
        if not self.token:
            raise RuntimeError("Cannot fetch accounts: Client is not authenticated.")

        # Gameforge lobby URLs use underscores for the hub path (e.g. /en_GB/hub)
        url_locale = self.locale.replace("-", "_")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Referer": f"{self.LOBBY_BASE}/{url_locale}/hub"
        }
        resp = self.session.get(f"{self.LOBBY_BASE}/api/users/me/accounts", headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch accounts. Status {resp.status_code}: {resp.text}")
        return resp.json()

    def get_servers(self) -> List[Dict]:
        """Fetches server metadata."""
        if not self.token:
            raise RuntimeError("Cannot fetch servers: Client is not authenticated.")

        headers = {"Authorization": f"Bearer {self.token}"}
        resp = self.session.get(f"{self.LOBBY_BASE}/api/servers", headers=headers)
        return resp.json()

    def get_login_link(self, account_id: int, serv_lang: str, serv_number: int, blackbox: str) -> str:
        """Requests the SSO redirect link for a specific world server."""
        if not self.token:
            raise RuntimeError("Client is not authenticated with Lobby.")

        url = f"{self.LOBBY_BASE}/api/users/me/loginLink"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Referer": f"{self.LOBBY_BASE}/{self.locale}/accounts",
            "Content-Type": "application/json"
        }
        payload = {
            "server": {"language": serv_lang, "number": serv_number},
            "clickedButton": "account_list",
            "id": account_id,
            "blackbox": blackbox
        }
        resp = self.session.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to obtain login link (HTTP {resp.status_code}): {resp.text}")

        data = resp.json()
        if "url" not in data:
            raise ValueError(f"Login link missing in response: {data}")
        return data["url"]