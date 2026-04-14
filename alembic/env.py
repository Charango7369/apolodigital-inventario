from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

from app.config import get_settings
from app.database import Base

# Descomenta cada módulo cuando lo crees:
# from app.modules.inventario import models as _  # noqa
# from app.modules.auth import models as _        # noqa

config = context.config
settings = get_settings()

config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata