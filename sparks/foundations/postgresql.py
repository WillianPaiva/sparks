# -*- coding: utf-8 -*-
"""
    PostgreSQL sparks helpers.

    For the Django developers to be able to create users & databases in
    complex architectures (eg. when the DB server is not on the Django
    instance server…) you must first define a PostgreSQL restricted admin
    user to manage the Django projects and apps databases.

    This user doens't need to be strictly ``SUPERUSER``, though.
    Having ``CREATEDB`` and ``CREATEUSER`` will suffice (but ``CREATEROLE``
    won't). For memories, this is how I created mine, on the central
    PostgreSQL server::

        # OPTIONAL: I first give me some good privileges
        # to avoid using the `postgres` system user.
        sudo su - postgres
        createuser --login --no-inherit \
            --createdb --createrole --superuser `whoami`
        psql
            ALTER USER `whoami` WITH ENCRYPTED PASSWORD 'MAKE_ME_STRONG';
        [exit]

        # Then, I create the other admin user which will handle all fabric
        # requests via developer tasks.
        psql
            CREATE ROLE oneflow_admin PASSWORD '<passwd>' \
                CREATEDB CREATEUSER NOINHERIT LOGIN;

        # NOTE: NOSUPERUSER conflicts with CREATEUSER.

        # Already done in previous command,
        # but keeing it here for memories.
        #    ALTER USER oneflow_admin WITH ENCRYPTED PASSWORD '<passwd>';

"""

import os
import pwd
import logging
from fabric.api import env
from ..fabric import with_remote_configuration, is_local_environment

LOGGER = logging.getLogger(__name__)

BASE_CMD    = '{pg_env} psql -tc "{sqlcmd}"'

# {pg_env} is intentionnaly repeated, it will be filled later.
# Without repeating it, `.format()` will fail with `KeyError`.
SELECT_USER = BASE_CMD.format(pg_env='{pg_env}',
                              sqlcmd="SELECT usename from pg_user "
                              "WHERE usename = '{user}';")
CREATE_USER = BASE_CMD.format(pg_env='{pg_env}',
                              sqlcmd="CREATE USER {user} "
                              "WITH PASSWORD '{password}';")
ALTER_USER  = BASE_CMD.format(pg_env='{pg_env}',
                              sqlcmd="ALTER USER {user} "
                              "WITH ENCRYPTED PASSWORD '{password}';")
SELECT_DB   = BASE_CMD.format(pg_env='{pg_env}',
                              sqlcmd="SELECT datname FROM pg_database "
                              "WHERE datname = '{db}';")
CREATE_DB   = BASE_CMD.format(pg_env='{pg_env}',
                              sqlcmd="CREATE DATABASE {db} OWNER {user};")


@with_remote_configuration
def get_admin_user(remote_configuration=None):

    environ_user = os.environ.get('SPARKS_PG_SUDO_USER', None)

    if environ_user is not None:
        LOGGER.info('Using environment variable SPARKS_PG_SUDO_USER.')
        return environ_user

    if is_local_environment():
        return pwd.getpwuid(os.getuid()).pw_name

    else:
        if remote_configuration.lsb:
            # FIXED: on Ubuntu / Debian, it's been `postgres` since ages.
            return 'postgres'

        raise NotImplementedError("Don't know how to find PG user "
                                  "on remote OSX server / Ubuntu.")


@with_remote_configuration
def temper_db_args(remote_configuration=None,
                   db=None, user=None, password=None):
    """ Try to accomodate with DB creation arguments.

        If all of them are ``None``, the function will try to fetch
        them automatically from the remote server Django settings.

    """

    if db is None and user is None and password is None:
        djsettings = getattr(remote_configuration, 'django_settings', None)

        if djsettings is None:
            raise ValueError('No database parameters supplied and no '
                             'remote Django settings available!')

        else:
            # if django settings has 'test' or 'production' (=env.environment)
            # DB, get it. Else get 'default' because all settings have it.
            db_settings = djsettings.DATABASES.get(
                env.environment, djsettings.DATABASES['default'])

            db       = db_settings['NAME']
            user     = db_settings['USER']
            password = db_settings['PASSWORD']

    if db is None:
        if user is None:
            raise ValueError('Parameters db and user '
                             'cannot be `None` together.')

        db = user

    else:
        if user is None:
            user = db

    if password is None:
        if not is_local_environment():
            raise ValueError('Refusing to set the username as password '
                             'in a production environment. Seriously?')

        password = user

    return db, user, password
