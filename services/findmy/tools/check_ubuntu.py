"""Non-secret deployment checks; does not log in or start collection."""
import asyncio
import json
from pathlib import Path
import re
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from localfindmy.storage import Vault
from localfindmy.transfer import seal, restore
from localfindmy.mac_anisette import MacAnisetteProvider
from localfindmy.network import check_network
from cryptography.exceptions import InvalidTag


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        vault = Vault(root)
        vault.write("probe", {"secret": "temporary-test-secret"})
        assert Vault(root).read("probe") == {"secret": "temporary-test-secret"}
        assert (root / ".vault-key").stat().st_mode & 0o777 == 0o600
        saved = root / "probe.vault"
        blob = saved.read_bytes()
        assert b"temporary-test-secret" not in blob
        saved.write_bytes(blob[:-1] + bytes([blob[-1] ^ 1]))
        try:
            vault.read("probe")
        except InvalidTag:
            pass
        else:
            raise AssertionError("Modified ciphertext was accepted")
        (root / ".vault-key").unlink()
        try:
            vault.read("probe")
        except RuntimeError:
            pass
        else:
            raise AssertionError("Missing key not detected")
        assert not (root / ".vault-key").exists()
        migration = seal({"devices": {}, "locations": {}, "schedule": {}}, "test-only-migration-password")
        assert restore(migration, "test-only-migration-password", root / "fresh") == {"devices": 0, "reports": 0}
        try:
            restore(migration, "test-only-migration-password", root / "fresh")
        except ValueError:
            pass
        else:
            raise AssertionError("Restore overwrote existing data")
    print("PASS Linux AES-GCM storage, tamper detection, missing-key protection and transfer", flush=True)
    provider = MacAnisetteProvider()
    assert provider._run("is_machine_provisioned", -2).ret == -45061
    print("PASS CoreADI/Unicorn offline initialization", flush=True)
    base = "http://127.0.0.1:8765"
    with urllib.request.urlopen(base, timeout=15) as r:
        html = r.read().decode()
    token = re.search(r'name="local-token" content="([^"]+)"', html)[1]
    def api(path):
        request = urllib.request.Request(base + path, headers={"X-Local-Token": token})
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    status = api("/api/status")
    devices = api("/api/devices")
    tasks = api("/api/schedule")["tasks"]
    print(json.dumps({"devices":len(devices), "tasks":len(tasks), "enabled_tasks":sum(t["enabled"] for t in tasks), "logged_in":status["logged_in"], "restore_error":status["restore_error"]}), flush=True)
    network = asyncio.run(check_network())
    print(json.dumps({"network":[{k:v for k,v in item.items() if k in ("name", "reachable", "http_status", "error")} for item in network["checks"]]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
