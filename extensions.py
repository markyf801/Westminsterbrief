from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter

from client_ip import client_ip

db = SQLAlchemy()
# Keyed on the visitor's real address, not a shared Cloudflare server's.
limiter = Limiter(key_func=client_ip, storage_uri="memory://")
