import os
import uvicorn
from .config import load

os.umask(0o077)
config = load()
uvicorn.run('mcore.api:app', host=config['host'], port=config['port'],
            ssl_certfile=config['tls_cert'], ssl_keyfile=config['tls_key'],
            access_log=False, workers=1)
