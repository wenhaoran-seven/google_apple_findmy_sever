"""One initialization-only probe, reporting status codes without credentials."""
import asyncio
import json
from pathlib import Path
import re
import sys
import traceback
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import localfindmy.app as app
from localfindmy.account import DiagnosticAppleAccount
from localfindmy.mac_anisette import AppleHttpSession
from localfindmy.storage import Vault
import srp


async def main():
    original = AppleHttpSession.request
    async def traced(self, method, url, **kwargs):
        result = await original(self, method, url, **kwargs)
        parsed = urlsplit(url)
        print(json.dumps({"method":method,"host":parsed.hostname,"path":parsed.path,"http_status":result.status_code}), flush=True)
        return result
    AppleHttpSession.request = traced
    saved = Vault(app.DATA).read("account")
    if not saved:
        print("No saved login attempt; no account request sent")
        return
    account = DiagnosticAppleAccount.from_json(saved)
    try:
        print(json.dumps({"saved_state":account.login_state.name,"has_username":bool(account._username)}), flush=True)
        if account.login_state.name != "LOGGED_OUT" or not account._username:
            return
        user = srp.User(account._username, b"", hash_alg=srp.SHA256, ng_type=srp.NG_2048)
        _, public = user.start_authentication()
        response = await account._gsa_request({"A2k":public,"u":account._username,"ps":["s2k","s2k_fo"],"o":"init"})
        status = response.get("Status",{})
        print(json.dumps({"stage":"init","code":status.get("ec") if type(status.get("ec")) is int else None,"hsc":status.get("hsc") if type(status.get("hsc")) is int else None,"supported_srp":response.get("sp") in ("s2k","s2k_fo"),"challenge_fields_present":all(k in response for k in ("s","B","c","i"))}), flush=True)
    except Exception as error:
        code = re.fullmatch(r"Error response for GSA request: ([0-9]{3})", str(error))
        print(json.dumps({"error_type":type(error).__name__,"gsa_http_status":int(code[1]) if code else None,"frames":[{"function":f.name,"line":f.lineno,"file":Path(f.filename).name} for f in traceback.extract_tb(error.__traceback__)[-5:]]}), flush=True)
    finally:
        await account.close()


asyncio.run(main())
