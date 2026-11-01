"""Root-only local systemd crash/recovery integration check."""
import subprocess
import time
import ssl
import tomllib
import urllib.error
import urllib.request


def property(name):
    return subprocess.check_output(['systemctl', 'show', 'mcore', '--property', name, '--value'], text=True).strip()


before, restarts = property('MainPID'), int(property('NRestarts'))
with open('/etc/mcore/config.toml', 'rb') as f:
    config = tomllib.load(f)
context = ssl.create_default_context(cafile=config['tls_cert'])
subprocess.run(['systemctl', 'kill', '--signal=SIGKILL', '--kill-whom=main', 'mcore'], check=True)
deadline = time.monotonic()+20
while time.monotonic()<deadline:
    if property('ActiveState')=='active' and property('MainPID') not in ('0', before) and int(property('NRestarts'))>restarts:
        try:
            urllib.request.urlopen(config['public_url']+'/v1/health', context=context, timeout=1)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                print('PASS: systemd restarted MCore after SIGKILL; HTTPS API is ready')
                break
        except (urllib.error.URLError, TimeoutError):
            pass
    time.sleep(.25)
else:
    raise SystemExit('MCore failed to restart')
