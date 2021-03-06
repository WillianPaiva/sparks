# -*- coding: utf-8 -*-
"""
    Fabric common rules for a Django project.

    Handles deployment and service installation / run via supervisor.

    Supported roles names:

    - ``web``: a gunicorn web server,
    - ``worker``: a simple celery worker (all queues),
    - ``worker_{low,medium,high}``: a combination
      of two or three celery workers (can be combined with
      simple ``worker`` too, for fine grained scheduling on
      small architectures),
    - ``flower``: a flower (celery monitoring) service,
    - ``shell``: an iPython notebooks shell service (on 127.0.0.1; up to
      you to get access to it via an SSH tunnel),

    For more information, jump to :class:`DjangoTask`.

"""

import os
import pwd
import logging
import datetime

try:
    from fabric.api              import (env, run, sudo, task,
                                         local, execute, serial)
    from fabric.tasks            import Task
    from fabric.operations       import put, prompt
    from fabric.contrib.files    import exists, upload_template, sed
    from fabric.context_managers import cd, prefix, settings

except ImportError:
    print('>>> FABRIC IS NOT INSTALLED !!!')
    raise

from ..fabric import (fabfile, with_remote_configuration,
                      local_configuration as platform,
                      is_local_environment,
                      is_development_environment,
                      is_production_environment,
                      execute_or_not, QUIET)
from ..pkg import brew, apt
from ..foundations import postgresql as pg
from ..foundations.classes import SimpleObject

# Use this in case paramiko seems to go crazy. Trust me, it can do, especially
# when using the multiprocessing module.
#
# logging.basicConfig(format=
#                     '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
#                     level=logging.INFO)

LOGGER = logging.getLogger(__name__)


# These can be overridden in local projects fabfiles.
env.requirements_dir      = 'config'
env.requirements_file     = os.path.join(env.requirements_dir,
                                         'requirements.txt')
env.dev_requirements_file = os.path.join(env.requirements_dir,
                                         'dev-requirements.txt')
env.branch                = '<GIT-FLOW-DEPENDANT>'
env.use_ssh_config        = True


# ••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••• Django task


class DjangoTask(Task):
    """ A Simple task wrapper that will ensure you are running your sparks
        Django tasks from near your :file:`manage.py`. This ensures that
        paths are always correctly set, which is too difficult to ensure
        otherwise.

        Sparks Django tasks assume the following project structure:

            $repository_root/
                config/
                    *requirements.txt
                $django_project_root/
                    settings/                 # or settings.py, to your liking.
                    $django_app1/
                    …
                manage.py
                fabfile.py
                Procfile.*

        .. versionadded:: in sparks 1.16.2. This is odd and doesn't conform
            to `Semantic Versioning  <http://semver.org/>`_. Sorry for that,
            it should have had. Next time it will do a better job.
    """

    def __init__(self, func, *args, **kwargs):
        super(DjangoTask, self).__init__(*args, **kwargs)
        self.func = func

    def __call__(self, *args, **kwargs):
        if not os.path.exists('./manage.py'):
            raise RuntimeError('You must run this task from where manage.py '
                               'is located, and this must be exactly in ../ '
                               'from your django project.')
        return self.func(*args, **kwargs)

    def run(self, *args, **kwargs):
        return self(*args, **kwargs)


class ServiceRunner(SimpleObject):
    """ Handle all the supervisor/upstart configuration and the
        restart/reload dirty work.

        .. versionchanged:: in 3.0, this class was renamed
            from ``SupervisorHelper`` to ``ServiceRunner`` with the
            added :program:`upstart` support.

        .. versionadded:: in sparks 2.0, all ``supervisor_*`` functions
            were merged into this controller, and support for Fabric's
            ``env.role`` was added.

    """

    def __init__(self, *args, **kwargs):
        # Too bad, SimpleObject is an old-style class (and must stay)
        SimpleObject.__init__(self, *args, **kwargs)

        self.update  = False
        self.restart = False

        if exists('/usr/bin/supervisorctl'):
            # testing exists('/etc/supervisor') isn't accurate: the
            # directory could still be there on a Debian/Ubuntu system,
            # even after a "remove --purge" (observed on obi.1flow.io).
            self.service_handler = 'supervisor'
        else:
            self.service_handler = 'upstart'

    @classmethod
    def build_program_name(cls, service=None):
        """ Returns a tuple: a boolean and a program name.

            The boolean indicates if the fabric `env` has
            the :attr:`sparks_djsettings` attribute. The program name
            will be somewhat unique, built from ``service``, ``env.project``,
            ``env.sparks_djsettings`` if it exists and ``env.environment``.

            :param service: a string describing the service.
                Defaults to Fabric's ``env.host_string.role`` (which is
                a sparks specific attributes, not yet merged into Fabric
                as os 2013-06) or ``env.sparks_current_role`` which is
                obviously sparks specific too, and exists when only the
                ``-H`` argument is given on command line.
                Can be anything meaningfull, eg. ``worker``, ``db``, etc.

        """

        role_name = getattr(env.host_string, 'role', None
                            ) or env.sparks_current_role

        if role_name is None:
            # This shouldn't happen, in fact. Either Fabric should have
            # set the first, or sparks' execute_or_not() the second.
            raise RuntimeError('WE HAVE NO ROLE, THIS IS MANDATORY.')

        if service is None:
            service = role_name

        # We need something more unique than project, in case we have
        # many environments on the same remote machine. And alternative
        # settings, too, because we will have a supervisor process for them.
        if hasattr(env, 'sparks_djsettings'):
            return True, '{0}_{1}_{2}_{3}'.format(service,
                                                  env.project,
                                                  env.sparks_djsettings,
                                                  env.environment)

        else:
            return False, '{0}_{1}_{2}'.format(service,
                                               env.project,
                                               env.environment)

    def add_environment_to_context(self, context, has_djsettings):
        """ Helper method: add (or not) an ``environment`` item
            to :param:`context`, given the current Fabric ``env``.

            If :param:`has_djsettings` is ``True``, ``SPARKS_DJANGO_SETTINGS``
            will be added.

            If ``env`` has an ``environment_vars`` attributes, they are assumed
            to be a python list (eg.``[ 'KEY1=value', 'KEY2=value2' ]``) and
            will be inserted into context too, converted to supervisor
            configuration file format.
        """

        env_vars = []

        if has_djsettings:
            env_vars.append(sparks_djsettings_env_var().strip())

        if hasattr(env, 'environment_vars'):
                env_vars.extend(env.environment_vars)

        if env_vars:
            context['environment'] = 'environment={0}'.format(
                ','.join(env_vars))

        else:
            # The item must exist, else the templating
            # engine will raise an error. Too bad.
            context['environment'] = ''

    def add_command_pre_post_to_context(self, context, has_djsettings):
        """ This method is called during the context build and before
            supervisor template rendering. It will set the context variables
            ``command_pre_args`` and ``command_post_args`` to empty values
            in the first time. Then it will call the
            method ``self.custom_context_handler()`` if it exists, passing
            a copy of the current context, a ``has_djsettings`` boolean and
            and the ``remote_configuration`` to it, in case handler needs to
            inspect current values. The custom handler should return the
            context copy, with ``command_{pre,post}_args`` modified to fit
            the needs.

            .. note:: the mechanism is not perfect, security wise, but sparks
                is not oriented towards strict security in its current
                incarnation. Abusing it would be non-sense anyway, because
                people using it already have sysadmin rights on remote machines
                on which sparks will run.

            .. note:: for ``remote_configuration`` to be passed; you need to
                pass it as an argument to the ServiceRunner constructor.
                See the celery handling part for an example.

            .. versionadded:: 2.6
        """

        # In all cases, these context variables must exist and be empty,
        # to avoid letting some unresolved `%(var)s` in templates.
        context.update({
            'command_pre_args': '',
            'command_post_args': '',
        })

        custom_handler = getattr(self, 'custom_context_handler', None)
        remote_configuration = getattr(self, 'remote_configuration', None)
        if custom_handler is None:
            return

        temp_context = custom_handler(context.copy(), has_djsettings,
                                      remote_configuration)

        context['command_pre_args']  = temp_context['command_pre_args']
        context['command_post_args'] = temp_context['command_post_args']

    def restart_or_reload(self):
        """

            .. versionchanged:: in 3.0, support :program:`upstart`.
        """
       # cf. http://stackoverflow.com/a/9310434/654755

        if self.update:
            if self.service_handler == 'upstart':
                sudo("initctl reload {0} "
                     "|| initctl reload-configuration".format(
                     self.program_name))
            else:
                sudo("supervisorctl update")

            if self.restart:
                if self.service_handler == 'upstart':
                    # In case of configuration change, 'restart' just reboots
                    # the previous configuration contents. We have to
                    # stop/start.
                    sudo("stop {0} ; start {0}".format(self.program_name))

                else:
                    sudo("supervisorctl restart {0}".format(self.program_name))

        else:
            # In any case, we restart the process during a {fast}deploy,
            # to reload the Django code even if configuration hasn't changed.

            if self.service_handler == 'upstart':
                sudo("stop {0} ; start {0}".format(self.program_name))
            else:
                sudo("supervisorctl restart {0}".format(self.program_name))

    def find_configuration_or_template(self, service_name=None):
        """ Return a tuple of candidate configuration files or templates
            for the given :param:`service_name`, which defaults
            to ``supervisor`` if not supplied.
        """

        if service_name is None:
            service_name = self.service_handler

        role_name = getattr(env.host_string, 'role', None
                            ) or env.sparks_current_role

        candidates = (
            os.path.join(platform.django_settings.BASE_ROOT,
                         'config', service_name,
                         '{0}.conf'.format(self.program_name)),

            os.path.join(platform.django_settings.BASE_ROOT,
                         'config', service_name,
                         '{0}.template'.format(role_name)),

            os.path.join(platform.django_settings.BASE_ROOT,
                         'config', service_name,
                         '{0}.conf'.format(role_name)),

            # Last resort: the sparks template
            os.path.join(os.path.dirname(__file__),
                         'templates', service_name,
                         '{0}.template'.format(role_name))
        )

        superconf = None

        # os.path.exists(): we are looking for a LOCAL file,
        # in the current Django project. Devops can prepare a
        # fully custom supervisor configuration file for the
        # Django web worker.
        for candidate in candidates:
            if os.path.exists(candidate):
                superconf = candidate
                break

        if superconf is None:
            raise RuntimeError('Could not find any configuration or '
                               'template for {0}. Searched {1}.'.format(
                               self.program_name, candidates))

        return superconf

    def configure_service(self, remote_configuration):
        """ Upload an environment-specific :program:`upstart`
            or :program:`supervisor` configuration file (depending on
            the ``remote_configuration`` parameter). The file is
            re-generated at each call in case configuration changed in
            the source repository.

            Upstart/Supervisor will be automatically restarted if
            configuration changed.

            Given ``root = remote_configuration.django_settings.BASE_ROOT``,
            this method will look for all these candidates (in order,
            first-match wins) for a given service:

                ${root}/config/${service}/${program_name}.conf
                ${root}/config/${service}/${role}.template
                ${root}/config/${service}/${role}.conf
                ${sparks_data_dir}/${service}/${role}.template

            The service template can end with either ``.conf`` or ``.template``.
            This is just for convenience: ``.template`` is more meaningful,
            but ``.conf`` is for consistency in the source repository. Whatever
            the name and suffix, all files will be treated the same (eg.
            rendered via Fabric's :func:`upload_template`).

            Templates are feeded with this context:

                context = {
                    'env': env.environment,
                    'root': env.root,
                    'user': env.user,
                    'branch': env.branch,
                    'project': env.project,
                    'program': self.program_name,
                    'user_home': env.user_home
                        if hasattr(env, 'user_home')
                        else remote_configuration.tilde,
                    'virtualenv': env.virtualenv,
                }

            Some **environment variables** can be added too
            (see :class:`add_environment_to_context` for details).

            In some specific cases, two other keys are added to context:

            - ``command_pre_args``
            - ``command_post_args``

            These two keys are just strings that will be prepended and suffixed
            to the final supervisor ``command`` directive.

            .. note:: this method assumes the remote machine is an
                Ubuntu/Debian server (physical or not), and will deploy
                supervisor configuration files
                to :file:`/etc/supervisor/conf.d/`.

            .. versionchanged:: Added :program:`upstart` support in version
                3.1. Prior to version 3.x, this method was named
                after ``configure_program``.

            .. versionchanged:: in version 2.6, the ``command_{pre,post}_args``
                were added, notably to handle installing more than one celery
                worker on the same machine.

        """

        superconf = self.find_configuration_or_template()

        # XXX/TODO: rename templates in sparks, create worker template.

        destination = '/etc/{0}/{1}.conf'.format(
            'init' if self.service_handler == 'upstart'
            else 'supervisor/conf.d', self.program_name)

        # NOTE: update docstring if you change this.
        context = {
            'env': env.environment,
            'root': env.root,
            'user': env.user,
            'branch': env.branch,
            'project': env.project,
            'program': self.program_name,
            'user_home': env.user_home
                if hasattr(env, 'user_home')
                else remote_configuration.tilde,
            'virtualenv': env.virtualenv,
        }

        self.add_environment_to_context(context, self.has_djsettings)
        self.add_command_pre_post_to_context(context, self.has_djsettings)

        if exists(destination):
            upload_template(superconf, destination + '.new',
                            context=context, use_sudo=True, backup=False)

            if sudo('diff {0} {0}.new'.format(destination),
                    warn_only=True) == '':
                sudo('rm -f {0}.new'.format(destination))

            else:
                sudo('mv {0}.new {0}'.format(destination))
                self.update  = True
                self.restart = True

        else:
            upload_template(superconf, destination, context=context,
                            use_sudo=True, backup=False)
            # No need to restart, the update will
            # add the new program and start it
            # automatically, thanks to supervisor.
            self.update = True

    def handle_gunicorn_config(self):
        """ Upload a gunicorn configuration file to the server. Principle
            is exactly the same as the supervisor configuration. Looked
            up paths are the similar, except that the method will look
            for them in the :file:`gunicorn/` subdir instead
            of :file:`supervisor/`.
        """

        guniconf = self.find_configuration_or_template('gunicorn')
        gunidest = os.path.join(env.root, 'config', 'gunicorn',
                                '{0}.conf'.format(self.program_name))

        # NOTE: as the configuration file stays in config/ — which is
        # is a git managed directory – and is not templated at all, we
        # are double-checking a file that is already good, most of the time.
        #
        # BUT, in case of a migration, where the developers just created
        # a new config file whereas before there wasn't any, this will
        # make the migration process appear natural; the user won't be
        # annoyed with a 'please move <file> out of the way' GIT message,
        # and won't be required to make a manual operation.

        if exists(gunidest):
            put(guniconf, gunidest + '.new')

            if sudo('diff {0} {0}.new'.format(gunidest),
                    warn_only=True) == '':
                sudo('rm -f {0}.new'.format(gunidest))

            else:
                sudo('mv {0}.new {0}'.format(gunidest))
                self.update  = True
                self.restart = True

        else:
            # copy the default configuration to remote.
            put(guniconf, gunidest)

            if not self.update:
                self.restart = True


# ••••••••••••••••••••••••••••••••••••••••••••••••••••• commands & global tasks


@task(aliases=('command', 'cmd'))
def run_command(cmd):
    """ Run a command on the remote side, inside the virtualenv and ch'ed
        into ``env.root``. Use like this (but don't do this in production):

        fab test cmd:'./manage.py reset admin --noinput'

        .. versionadded:: in 2.0.
    """

    # Wrap the real task to eventually run on all hosts it none specified.
    execute_or_not(run_command_task, cmd)


@task
def sed_command_task(*args, **kwargs):

    with activate_venv():
        with cd(env.root):
            sed(*args, **kwargs)


@task(aliases=('sed', ))
def sed_command(*args, **kwargs):
    """ Run a Fabric sed command on the remote side, inside the virtualenv
        and ch'ed into ``env.root``. Use like this (but don't do this in
        production)::

            # In fact, this WON'T work because of spaces and equals signs.
            #   fab test sed:.git/config,'url = olive@','url = git@'
            # You should try avoiding them, and this should work:
            fab test sdf.sed:.git/config,'(url.*)olive@','\1git@'

        Reminder of Fabric 1.6.1 ``sed`` function arguments::

            filename, before, after, limit='', use_sudo=False,
            backup='.bak', flags='', shell=False

        .. versionadded:: in 2.8.
    """

    # Wrap the real task to eventually run on all hosts it none specified.
    execute_or_not(sed_command_task, *args, **kwargs)


@task
def run_command_task(cmd):

    with activate_venv():
        with cd(env.root):
            run(cmd)


@task(aliases=('base', 'base_components'))
@with_remote_configuration
def install_components(remote_configuration=None, upgrade=False):
    """ Install necessary packages to run a full Django stack.

        .. todo:: terminate this task. It is not usable yet, except on
            an OSX development-only machine. Others (servers, test &
            production) are not implemented yet and require manual
            installation / configuration.

            - split me into packages/modules where appropriate.
            - split me into server and clients packages.

        .. note:: server configuration / deployment can nevertheless be
            leveraged by:

            - a part of ``sparks.fabfile.*`` which contains server tasks,
            - and by the fact that many Django services are managed by
              the project requirements (thus installed automatically) and
              via supervisord. Thus, on the worker/web side, only
              supervisord requires to be installed. On other machines,
              redis/memcached/PostgreSQL/MongoDB and friends remain to
              be loved by your sysadmin skills.
    """

    LOGGER.info('Checking installed components…')

    with activate_venv():
        with cd(env.root):
            fabfile.dev()
            fabfile.dev_web()
            fabfile.dev_django_full()

    # OSX == test environment == no nginx/supervisor/etc
    if remote_configuration.is_osx:

        LOGGER.warning('Considering a development environment, '
                       'installing everything on OSX.')

        brew.brew_add(('nginx', ))

        # If you want to host pages on your local machine to the wider network
        # you can change the port to 80 in: /usr/local/etc/nginx/nginx.conf
        # You will then need to run nginx as root: `sudo nginx`.

        run('ln -sfv /usr/local/opt/nginx/*.plist ~/Library/LaunchAgents',
            quiet=QUIET)
        run('launchctl load ~/Library/LaunchAgents/homebrew.mxcl.nginx.plist',
            quiet=QUIET)

        fabfile.db_redis()
        fabfile.db_postgresql()
        fabfile.db_mongodb()
        fabfile.db_memcached()

        # Already done in dev_django_full()
        #fabfile.dev_memcache()

        # 'rabbitmq'
        # run('ln -sfv /usr/local/opt/rabbitmq/*.plist ~/Library/LaunchAgents',
        #     quiet=QUIET)
        # run('launchctl load ~/Library/LaunchAgents/homebrew.*.rabbitmq.plist',
        #     quiet=QUIET)

    else:
        current_role = getattr(env.host_string, 'role', None
                               ) or env.sparks_current_role

        if current_role.startswith('worker'):
            apt.apt_add(('supervisor', ))

        if current_role == 'web':
            apt.apt_add(('supervisor', 'nginx-full', ))

        if is_local_environment():
            LOGGER.info('Installing all services for a local development '
                        'environment…')

            # These are duplicated here in case env.host_string has been
            # manually set to localhost in fabfile, which I do myself.
            apt.apt_add(('nginx-full', ))

            fabfile.db_redis()
            fabfile.db_memcached()
            fabfile.db_postgresql()
            fabfile.db_mongodb()

            # Already done in dev_django_full()
            #fabfile.dev_memcache()

        else:
            LOGGER.warning('NOT installing redis/PostgreSQL/MongoDB/Memcache '
                           'on anything other than local developement envs.')

# ••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••• Helpers


def get_git_branch():
    """ Return either ``env.branch`` if defined, else ``master`` if environment
        is ``production``, or ``develop`` if anything else than ``production``
        (we use the :program:`git-flow` branching model). """

    branch = env.branch

    if branch == '<GIT-FLOW-DEPENDANT>':
        branch = 'master' if env.environment == 'production' else 'develop'

    return branch


class activate_venv(object):
    """ Activate the virtualenv at the Fabric level.

        Additionnaly, try to deal gently with virtualenvwrapper's
        :file:`.project` file, which totally borks the remote path
        and anihilates Fabric's ``cd()`` benefits in normal conditions.

        For performance reasons, remote calls are done only at first call.
        If for some reason you would like to refresh the class values, you
        should file a pull request and implement the needed work
        in the :meth:`__call__` method.

        .. versionadded:: 2.5.
    """

    # Keep them as class objects, they never change…
    project_file = None
    has_project  = None

    def __init__(self):

        if activate_venv.has_project is None:
            workon_home = run('echo $WORKON_HOME', quiet=QUIET).strip() \
                or '${HOME}/.virtualenvs'

            activate_venv.project_file = os.path.join(workon_home,
                                                      env.virtualenv,
                                                      '.project')
            activate_venv.has_project  = exists(activate_venv.project_file)

        self.my_prefix = prefix('workon %s' % env.virtualenv)

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        if activate_venv.has_project:
            run('mv "{0}" "{0}.disabled"'.format(activate_venv.project_file),
                quiet=QUIET)

        self.my_prefix.__enter__()

    def __exit__(self, *args, **kwargs):

        self.my_prefix.__exit__(*args, **kwargs)

        if activate_venv.has_project:
            run('mv "{0}.disabled" "{0}"'.format(activate_venv.project_file),
                quiet=QUIET)


def sparks_djsettings_env_var():

    # The trailing space is intentional. Callers expect us to have inserted
    # it if we setup the shell environment variable.
    return 'SPARKS_DJANGO_SETTINGS={0} '.format(
        env.sparks_djsettings) if hasattr(env, 'sparks_djsettings') else ''


def django_settings_env_var():

    # The trailing space is intentional. Callers expect us to have inserted
    # it if we setup the shell environment variable.
    return 'DJANGO_SETTINGS_MODULE={0}.settings '.format(
        env.project) if hasattr(env, 'project') else ''


def get_all_fixtures(order_by=None):
    """ Find all fixtures files in the current project, eg. files whose name
        ends with ``.json`` and which are located in any `fixtures/` directory.

        :param order_by: a string. Currently only ``'date'`` is supported.

        .. note:: the action takes place on the current machine, eg. it uses
            ``Fabric's`` :func:`local` function.

        .. versionadded:: 1.16
    """

    # OMG: http://stackoverflow.com/a/11456468/654755 ILOVESO!

    if order_by is None:
        return local("find . -name '*.json' -path '*/fixtures/*'",
                     capture=True).splitlines()

    elif order_by == 'date':
        return local("find . -name '*.json' -path '*/fixtures/*' -print0 "
                     "| xargs -0 ls -1t", capture=True).splitlines()

    else:
        raise RuntimeError('Bad order_by value "{0}"'.format(order_by))


def new_fixture_filename(app_model):
    """

        .. versionadded:: 1.16
    """

    def fixture_name(base, counter):
        return '{0}_{1:04d}.json'.format(base, counter)

    try:
        app, model = app_model.split('.', 1)

    except ValueError:
        app   = app_model
        model = None

    fixtures_dir = os.path.join(env.project, app, 'fixtures')

    if not os.path.exists(fixtures_dir):
        os.makedirs(fixtures_dir)

    # WARNING: no dot '.' in fixtures names, else Django fails to install it.
    # 20130514: CommandError: Problem installing fixture 'landing':
    # 2013-05-14_0001 is not a known serialization format.
    new_fixture_base = os.path.join(fixtures_dir, '{0}{1}_{2}'.format(app,
                                    '' if model is None else ('.' + model),
                                    datetime.date.today().isoformat()))

    fix_counter = 1
    new_fixture_name = fixture_name(new_fixture_base, fix_counter)

    while os.path.exists(new_fixture_name):
        fix_counter += 1
        new_fixture_name = fixture_name(new_fixture_base, fix_counter)

    return new_fixture_name

# •••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••• Code related


@task
def init_environment():
    """ Create ``env.root`` on the remote side, and the ``env.virtualenv``
        it they do not exist.

        if ``env.repository`` exists, the following command will be run
        automatically::

            git clone ${env.repository} ${env.root}

        Else, the user will be prompted to create the repository manually
        before continuing.

    """

    LOGGER.info('Checking base environment…')

    if not exists(env.root):
        run('mkdir -p "{0}"'.format(os.path.dirname(env.root)))

        if hasattr(env, 'repository'):
            run("git clone {0} {1}".format(env.repository, env.root))

        else:
            prompt(u'Please create the git repository in {0}:{1} and press '
                   u'[enter] when done.\nIf you want it to be cloned '
                   u'automatically, just define `env.repository` in '
                   u'your fabfile.'.format(env.host_string, env.root))

    if run('lsvirtualenv | grep {0}'.format(env.virtualenv),
           warn_only=True).strip() == '':
        run('mkvirtualenv {0}'.format(env.virtualenv))


@task
def pre_requirements_task(fast=False, upgrade=False):

    if is_local_environment():
        return

    role_name = getattr(env.host_string, 'role', None
                        ) or env.sparks_current_role

    custom_script = os.path.join(env.root, env.requirements_dir,
                                 role_name + '.sh')

    has_custom_script = exists(custom_script)

    if not has_custom_script:
        return

    LOGGER.info('Running custom requirements script (preinstall)…')

    with cd(env.root):
        with activate_venv():
            run('bash "{0}" preinstall "{1}" "{2}" "{3}" "{4}"'.format(
                custom_script, env.environment, env.virtualenv,
                role_name, env.host_string))


@task
def post_requirements_task(fast=False, upgrade=False):

    #
    # TODO: factorize role_name and exists() with pre_requirements_task
    #

    if is_local_environment():
        return

    role_name = getattr(env.host_string, 'role', None
                        ) or env.sparks_current_role

    custom_script = os.path.join(env.root, env.requirements_dir,
                                 role_name + '.sh')

    has_custom_script = exists(custom_script)

    if not has_custom_script:
        return

    LOGGER.info('Running custom requirements script (install)…')

    with cd(env.root):
        with activate_venv():
            run('bash "{0}" install "{1}" "{2}" "{3}" "{4}"'.format(
                custom_script, env.environment, env.virtualenv,
                role_name, env.host_string))


def requirements_task(fast=False, upgrade=False):

    # Thanks http://stackoverflow.com/a/9362082/654755
    if upgrade:
        command = 'yes w | {sparks_env}{django_env} pip install -U'
    else:
        command = 'yes w | {sparks_env}{django_env} pip install'

    command = command.format(sparks_env=sparks_djsettings_env_var(),
                             django_env=django_settings_env_var())

    with cd(env.root):
        with activate_venv():

            if is_development_environment():

                LOGGER.info('Checking development requirements…')

                dev_req = os.path.join(env.root, env.dev_requirements_file)

                if exists(dev_req):
                    run("{command} --requirement {requirements_file}".format(
                        command=command, requirements_file=dev_req))

            LOGGER.info('Checking requirements…')

            req = os.path.join(env.root, env.requirements_file)

            if exists(req):
                run("{command} --requirement {requirements_file}".format(
                    command=command, requirements_file=req))

            LOGGER.info('Done checking requirements.')


@task(alias='req')
def requirements(fast=False, upgrade=False):
    """ Install PIP requirements (and dev-requirements).

        .. note:: :param:`fast` is not used yet, but exists for consistency
            with other fab tasks which handle it.
    """

    roles_to_run = ('web', 'db', 'worker',
                    'worker_low', 'worker_medium', 'worker_high')

    for role in roles_to_run:
        execute_or_not(pre_requirements_task, fast=fast,
                       upgrade=upgrade, sparks_roles=(role, ))

    # re-wrap the internal task via execute() to catch roledefs.
    execute_or_not(requirements_task, fast, upgrade,
                   sparks_roles=roles_to_run)

    for role in roles_to_run:
        execute_or_not(post_requirements_task, fast=fast,
                       upgrade=upgrade, sparks_roles=(role, ))


def push_environment_task(project_envs_dir):

    role_name = getattr(env.host_string, 'role', None
                        ) or env.sparks_current_role

    for env_file_candidate in (
        '{0}.env'.format(env.host_string.lower()),
        '{0}_{0}.env'.format(role_name, env.environment),
        '{0}.env'.format(env.environment),
            'default.env', ):
        candidate_fullpath = os.path.join(project_envs_dir, env_file_candidate)

        if os.path.exists(candidate_fullpath):
            put(candidate_fullpath, '.env')
            return

    raise RuntimeError('$SPARKS_ENV_DIR is defined but no environment file '
                       'matched {0} in {1}!'.format(env.host_string,
                       project_envs_dir))


@task(task_class=DjangoTask)
def push_environment():
    """ Copy any environment file to the remote server in ``~/.env``,
        ready to be loaded by the shell when the user does anything.

        Environment files are shell scripts, they should be loaded
        via ``. ~/.env`` on the remote host.

        Master environment dir is indicated to :program:`sparks` via
        the environment variable ``SPARKS_ENV_DIR``. In this directory,
        sparks will look for a subdirectory named after
        Fabric's ``env.project``.

        In the project directory, *sparks* will lookup, in this order of
        preferences:

        - ``<remote_hostname_in_lowercase>.env`` (eg. ``1flow.io.env``)
        - ``<remote_host_role>_<env.environment>.env``
          (eg. ``web_production.env``)
        - ``<env.environment>.env`` (eg. ``production.env``)
        - ``default.env`` (in case you have only one environment file)

        The first that matches is the one that will be pushed.

        There is no kind of inclusion nor concatenation mechanism for now.

        .. versionadded:: 3.0

    """

    envs_dir = os.environ.get('SPARKS_ENV_DIR', None)

    if envs_dir is None:
        LOGGER.warning('$SPARKS_ENV_DIR is not defined, will not push any '
                       'environment file to any remote host.')
        return

    project_envs_dir = os.path.join(envs_dir, env.project)

    if not os.path.exists(project_envs_dir):
        LOGGER.warning('$SPARKS_ENV_DIR/{0} does not exist. Will not push any '
                       'environment file to any remote host.'.format(
                       env.project))
        return

    # re-wrap the internal task via execute() to catch roledefs.
    execute_or_not(push_environment_task, project_envs_dir,
                   sparks_roles=('web', 'db', 'worker',
                   'worker_low', 'worker_medium', 'worker_high'))


@task(alias='update')
def git_update():
    """ Push latest code from local to origin, checkout branch on remote. """

    with cd(env.root):
        if not is_local_environment():
            run('git checkout %s' % get_git_branch())


@serial
@task(alias='pull')
def git_pull():
    """ Pull latest code from origin to remote,
        reload sparks settings if changes.

        Serial task to avoid git lock conflicts on central repository.
    """

    with cd(env.root):
        if not run('git pull').strip().endswith('Already up-to-date.'):
            # reload the configuration to refresh Django settings.
            # TODO: examine commits HERE and in push_translations()
            # to reload() only if settings/* changed.
            #
            # We import it manually here, to avoid using the
            # @with_remote_configuration decorator, which would imply
            # implicit fetching of Django settings. On first install/deploy,
            # this would fail because requirements are not yet installed.
            try:
                from ..fabric import remote_configuration
                remote_configuration.reload()

            except:
                LOGGER.exception('Cannot reload remote_settings! '
                                 '(you can safely ignore this warning on '
                                 'first deploy)')


@serial
@task(alias='pull')
def git_clean():
    """ clean old Python compiled files. To avoid crashes like this one:

        http://dev.1flow.net/webapps/obi1flow/group/783/

        Which occured after removing profiles/admin.py and emptying models.py
        but admin.pyc was left il place and refered to an ancient model…
    """

    with cd(env.root):
        run("find . -name '*.pyc' -or -name '*.pyo' -print0 "
            " | xargs -0 rm -f", warn_only=True)


@task(alias='getlangs')
@with_remote_configuration
def push_translations(remote_configuration=None):
    """ If new gettext translations are available on remote, commit and push them. """ # NOQA

    try:
        if not remote_configuration.django_settings.DEBUG:
            # remote translations are fetched only on development / test
            # environments. Production are not meant to host i18n work.
            return

    except AttributeError:
        LOGGER.warning('push_translations() ignored, remote Django settings '
                       'cannot be loaded (you can ignore this warning during '
                       'first deployment.')
        return

    LOGGER.info('Checking for new translations…')

    with cd(env.root):
        if run("git status | grep -E 'modified:.*locale.*django.po' "
               "|| true") != '':
            run(('git add -u \*locale\*po '
                '&& git commit -m "{0}" '
                # If there are pending commits in the central, `git push` will
                # fail if we don't pull them prior to pushing local changes.
                '&& (git up || git pull) && git push').format(
                'Automated l10n translations from {0} on {1}.').format(
                env.host_string, datetime.datetime.now().isoformat()))

            # Get the translations changes back locally.
            # Don't fail if the local user doesn't have git-up,
            # and try to pull the standard way.
            local('git up || git pull')


# •••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••• Services


@task(alias='nginx')
def restart_nginx(fast=False):
    """ Restart the remote nginx (if installed), after having refreshed its configuration file. """ # NOQA

    if not exists('/etc/nginx'):
        return

    # Nothing implemented yet.
    return


@task(task_class=DjangoTask, alias='gunicorn')
@with_remote_configuration
def restart_webserver_gunicorn(remote_configuration=None, fast=False):
    """ (Re-)upload configuration files and reload gunicorn via supervisor.

        This will reload only one service, even if supervisor handles more
        than one on the remote server. Thus it's safe for production to
        reload test :-)

    """

    has_djsettings, program_name = ServiceRunner.build_program_name()

    service_runner = ServiceRunner(from_dict={
                                   'has_djsettings': has_djsettings,
                                   'program_name': program_name
                                   })

    if not fast:
        service_runner.configure_service(remote_configuration)
        service_runner.handle_gunicorn_config()

    service_runner.restart_or_reload()


def worker_options(context, has_djsettings, remote_configuration):
    """ This is the celery custom context handler. It will add
        the ``--hostname`` argument to the celery command line, as suggested
        at http://docs.celeryproject.org/en/latest/userguide/workers.html#starting-the-worker """ # NOQA

    def many_workers_on_same_host():
        hostname = env.host_string
        wcount   = 0

        for key, value in env.roledefs.items():
            if key.startswith('worker'):
                if hostname in value:
                    wcount += 1
                    if wcount > 1:
                        return True

        return wcount > 1

    command_pre_args  = ''
    command_post_args = ''

    role_name = getattr(env.host_string, 'role', None
                        ) or env.sparks_current_role

    # NOTE: the final '_' is intentional: exclude the simple 'worker' role.
    if role_name.startswith('worker_'):
        if many_workers_on_same_host():
            command_post_args += '--hostname {0}.{1}'.format(
                role_name, env.host_string
            )

    # NOTE: the void of '_' is intentional: all worker-related roles
    if role_name.startswith('worker'):
        sparks_options = getattr(env, 'sparks_options', {})
        worker_concurrency = sparks_options.get('worker_concurrency', {})

        # TODO: '5' should be 'if remote_configuration.is_lxc'
        # but we don't have this configuration attribute yet.

        command_post_args += ' -c {0}'.format(
            worker_concurrency.get(role_name, 5))

        max_tasks_per_child = sparks_options.get('max_tasks_per_child', {})

        if max_tasks_per_child:
            command_post_args += ' --maxtasksperchild={0}'.format(
                max_tasks_per_child.get(role_name,
                                        max_tasks_per_child.get(
                                        '__all__', 500)))

    context.update({
        'command_pre_args': command_pre_args,
        'command_post_args': command_post_args,
    })

    return context


@task(task_class=DjangoTask, alias='celery')
@with_remote_configuration
def restart_worker_celery(remote_configuration=None, fast=False):
    """ (Re-)upload configuration files and reload celery via supervisor.

        This will reload only one service, even if supervisor handles more
        than one on the remote server. Thus it's safe for production to
        reload test :-)

    """

    has_djsettings, program_name = ServiceRunner.build_program_name()

    service_runner = ServiceRunner(from_dict={
                                   'has_djsettings': has_djsettings,
                                   'program_name': program_name,
                                   'custom_context_handler': worker_options,
                                   'remote_configuration':
                                   remote_configuration,
                                   })

    if not fast:
        service_runner.configure_service(remote_configuration)
        # NO need:
        #   service_runner.handle_celery_config(<role>)

    service_runner.restart_or_reload()


# •••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••• Django tasks

@task(task_class=DjangoTask, alias='manage')
def django_manage(command, prefix=None, **kwargs):
    """ Calls a remote :program:`./manage.py`. Obviously, it will setup all
        the needed shell environment for the call to succeed.

        Not meant for complex calls (eg. makemessages in different directories
        than the project root). If you need more flexibility, call the command
        yourself, like ``sparks`` does in the :func:`handlemessages` function.

        :param command: the django manage command as a simple string,
            eg. ``'syncdb --noinput'``. Default: ``None``, but you must
            provide one, else manage will print its help (at best).

        :param prefix: a string that will be inserted at the start of the
            final command. For a badly implemented command which doesn't
            accept the ``--noinput`` argument, you can use ``prefix='yes | '``.
            Don't forget spaces if you want readability, the prefix will be
            inserted verbatim. Default: ``''``.

        :param kwargs: the remaining arguments are passed to
            fabric's :func:`run` method via the ``**kwargs`` mechanism.

        .. versionadded:: 1.16.

    """

    if prefix is None:
        prefix = ''

    with activate_venv():
        with cd(env.root):
            return run('{0}{1}./manage.py {2} --verbosity 1 --traceback'.format(
                       prefix, sparks_djsettings_env_var(), command), **kwargs)


@with_remote_configuration
def handlemessages(remote_configuration=None, mode=None):
    """ Run the Django compilemessages management command.

        .. note:: not a Fabric task, but a helper function.
    """

    if mode is None:
        mode = 'make'

    elif mode not in ('make', 'compile'):
        raise RuntimeError(
            '"mode" argument must be either "make" or "compile".')

    def compile_internal(run_from):
        for language in languages:
            run('{0}{1}./manage.py {2}messages --locale {3}'.format(
                sparks_djsettings_env_var(), run_from, mode, language),
                quiet=QUIET)

    # Transform language codes (eg. 'fr-fr') to locale names (eg. 'fr_FR'),
    # keeping extensions (eg. '.utf-8'), but don't touch short codes (eg. 'en').
    languages = [('{0}_{1}{2}'.format(code[:2], code[3:5].upper(), code[5:])
                 if len(code) > 2 else code) for code, name
                 in remote_configuration.django_settings.LANGUAGES
                 if code != remote_configuration.django_settings.LANGUAGE_CODE]

    project_apps = [app.split('.', 1)[1] for app
                    in remote_configuration.django_settings.INSTALLED_APPS
                    if app.startswith('{0}.'.format(env.project))]

    with activate_venv():
        with cd(env.root):
            with cd(env.project):
                if exists('locale'):
                    compile_internal(run_from='../')

                else:
                    for short_app_name in project_apps:
                        with cd(short_app_name):
                            LOGGER.info('Compiling language files for app %s…',
                                        short_app_name)
                            compile_internal(run_from='../../')


@task(task_class=DjangoTask, alias='messages')
def makemessages():
    handlemessages(mode='make')


@task(task_class=DjangoTask, alias='compile')
def compilemessages():
    handlemessages(mode='compile')


@task(task_class=DjangoTask)
@with_remote_configuration
def createdb(remote_configuration=None, db=None, user=None, password=None,
             installation=False):
    """ Create the PostgreSQL user & database if they don't already exist.
        Install PostgreSQL on the remote system if asked to. """

    LOGGER.info('Checking database setup…')

    if installation:
        from ..fabric import fabfile
        fabfile.db_postgresql()

    db, user, password = pg.temper_db_args(db=db, user=user, password=password)

    if is_local_environment():
        pg_env = []

    else:
        SPARKS_PG_SUPERUSER = os.environ.get('SPARKS_PG_SUPERUSER', None)
        SPARKS_PG_SUPERPASS = os.environ.get('SPARKS_PG_SUPERPASS', None)
        SPARKS_PG_TMPL_DB   = os.environ.get('SPARKS_PG_TMPL_DB',   None)

        pg_env = ['PGUSER={0}'.format(SPARKS_PG_SUPERUSER)
                  if SPARKS_PG_SUPERUSER else '',
                  'PGPASSWORD={0}'.format(SPARKS_PG_SUPERPASS)
                  if SPARKS_PG_SUPERPASS else '']

    pg_env.append('PGDATABASE={0}'.format(SPARKS_PG_TMPL_DB or 'template1'))

    djsettings = getattr(remote_configuration, 'django_settings', None)

    if djsettings is not None:
        db_setting = djsettings.DATABASES['default']
        db_host    = db_setting.get('HOST', None)
        db_port    = db_setting.get('PORT', None)

        if db_host is not None:
            pg_env.append('PGHOST={0}'.format(db_host))

        if db_port is not None:
            pg_env.append('PGPORT={0}'.format(db_port))

    # flatten the list
    pg_env = ' '.join(pg_env)

    with settings(sudo_user=pg.get_admin_user()):

        # WARNING: don't .strip() here, else we fail Fabric's attributes.
        db_user_result = sudo(pg.SELECT_USER.format(
                pg_env=pg_env, user=user), warn_only=True)

        if db_user_result.failed:
            if is_local_environment():
                raise RuntimeError('Is your local user account `{0}` a '
                                   'PostgreSQL administrator? it shoud be. '
                                   'To acheive it, please run:{1}'.format(
                                   pwd.getpwuid(os.getuid()).pw_name, '''
    sudo su - postgres
    USER=<your-username-here>
    PASS=<your-password-here>
    createuser --login --no-inherit --createdb --createrole --superuser ${USER}
    echo "ALTER USER ${USER} WITH ENCRYPTED PASSWORD '${PASS}';" | psql
    [exit]
'''))
            else:
                raise RuntimeError('Your remote system lacks a dedicated '
                                   'PostgreSQL administrator account. Did '
                                   'you create one? You can specify it via '
                                   'environment variables $SPARKS_PG_SUPERUSER '
                                   ' and $SPARKS_PG_SUPERPASS. You can also '
                                   'specify $SPARKS_PG_TMPL_DB (defaults to '
                                   '“template1” if unset, which is safe).')

        if db_user_result.strip() in ('', 'Password:'):
            sudo(pg.CREATE_USER.format(
                 pg_env=pg_env, user=user, password=password))
        else:
            sudo(pg.ALTER_USER.format(pg_env=pg_env,
                 user=user, password=password))

        if sudo(pg.SELECT_DB.format(pg_env=pg_env, db=db)).strip() == '':
            sudo(pg.CREATE_DB.format(pg_env=pg_env, db=db, user=user))

    LOGGER.info('Done checking database setup.')


@task(task_class=DjangoTask)
def syncdb():
    """ Run the Django syndb management command. """

    with activate_venv():
        with cd(env.root):
            # TODO: this should be managed by git and the developers, not here.
            run('chmod 755 manage.py', quiet=True)

    django_manage('syncdb --noinput')


@task(task_class=DjangoTask)
@with_remote_configuration
def migrate(remote_configuration=None, args=None):
    """ Run the Django migrate management command, and the Transmeta one
        if ``django-transmeta`` is installed.

        .. versionchanged:: in 1.16 the function checks if ``transmeta`` is
            installed remotely and runs the command properly. before, it just
            ran the command inconditionnaly with ``warn_only=True``, which
            was less than ideal in case of a problem because the fab procedure
            didn't stop.
    """

    django_manage('migrate ' + (args or ''))

    if 'transmeta' in remote_configuration.django_settings.INSTALLED_APPS:
        django_manage('sync_transmeta_db', prefix='yes | ')


@task(task_class=DjangoTask, alias='static')
@with_remote_configuration
def collectstatic(remote_configuration=None, fast=True):
    """ Run the Django collectstatic management command. If :param:`fast`
        is ``False``, the ``STATIC_ROOT`` will be erased first. """

    if remote_configuration.django_settings.DEBUG:
        LOGGER.info('NOT running collectstatic on %s because `DEBUG=True`.',
                    env.host_string)
        return

    if not fast:
        with cd(env.root):
            run('rm -rf "{0}"'.format(
                remote_configuration.django_settings.STATIC_ROOT))

    django_manage('collectstatic --noinput')


# ••••••••••••••••••••••••••••••••••••••••••••••••••••••••• Direct-target tasks


def putdata_task(filename=None, confirm=True, **kwargs):

    if filename is None:
        filename = get_all_fixtures(order_by='date')[0]

        if confirm:
            prompt('OK to load {0} ([enter] or Control-C)?'.format(filename))

    remote_file = list(put(filename))[0]

    django_manage('loaddata {0}'.format(remote_file))


@task(task_class=DjangoTask)
def putdata(filename=None, confirm=True):
    """ Load a local fixture on the remote via Django's ``loaddata`` command.
    """

    # re-wrap the internal task via execute() to catch roledefs.
    execute_or_not(putdata_task, filename=filename, confirm=confirm,
                   sparks_roles=('db', ))


def getdata_task(app_model, filename=None, **kwargs):

    if filename is None:
        filename = new_fixture_filename(app_model)
        print('Dump data stored in {0}'.format(filename))

    with open(filename, 'w') as f:
        f.write(django_manage('dumpdata {0} --indent 4 '
                '--format json --natural'.format(app_model), quiet=True))


@task(task_class=DjangoTask)
def getdata(app_model, filename=None):
    """ Get a dump or remote data in a local fixture,
        via Django's ``dumpdata`` management command.

        Examples::

            # more or less abstract examples
            fab test getdata:myapp.MyModel
            fab production custom_settings getdata:myapp.MyModel

            # The 1flowapp.com landing page.
            fab test oneflowapp getdata:landing.LandingContent

        .. versionadded:: 1.16
    """

    # re-wrap the internal task via execute() to catch roledefs.
    execute_or_not(getdata_task, app_model=app_model,
                   filename=filename, sparks_roles=('db', ))


@task(aliases=('maintenance', 'maint', ))
def maintenance_mode(fast=True):
    """ Trigger maintenance mode (and restart services). """

    result = execute_or_not(maintenance_mode_task, fast=fast,
                            sparks_roles=('web', ))

    if result is None:
        return

    if any(result.values()):
        restart_services(fast=fast)


@task
def maintenance_mode_task(fast):

    with cd(env.root):
        if exists('MAINTENANCE_MODE'):
            LOGGER.info('Already in maintenance mode, not restarting services.')
            return False

        run('touch MAINTENANCE_MODE', quiet=QUIET)
        return True


@task(aliases=('operational', 'op', 'normal', 'resume', 'run', ))
def operational_mode(fast=True):
    """ Get out of maintenance mode (and restart services). """

    result = execute_or_not(operational_mode_task, fast=fast,
                            sparks_roles=('web', ))

    if result is None:
        return

    if any(result.values()):
        restart_services(fast=fast)


@task
def operational_mode_task(fast):

    with cd(env.root):
        if exists('MAINTENANCE_MODE'):
            run('rm -f  MAINTENANCE_MODE', quiet=QUIET)
            return True
        else:
            LOGGER.info('Already in operational mode, not restarting services.')
            return False


# ••••••••••••••••••••••••••••••••••••••••••••••••••••••• Deployment meta-tasks


@task(alias='restart')
def restart_services(fast=False):
    """ Restart all remote services (nginx, gunicorn, celery…) in one task. """

    if is_local_environment():
        LOGGER.warning('Not restarting services, this is a local environment '
                       'and should be managed via Honcho.')
        return

    execute_or_not(restart_nginx, fast=fast, sparks_roles=('load', ))
    execute_or_not(restart_webserver_gunicorn, fast=fast,
                   sparks_roles=('web', ))

    roles_to_restart = ('worker',
                        'worker_low', 'worker_medium', 'worker_high', )

    if not fast:
        roles_to_restart += ('flower', 'shell', )

    # Run this multiple time, for each role:
    # each of them has a dedicated supervisor configuration,
    # even when running on the same machine.
    # Degrouping role execution ensures execute_or_not() gets an unique
    # role for each host it will execute on. This is a limitation of the
    # the execute_or_not() function.
    for role in roles_to_restart:
        execute_or_not(restart_worker_celery, fast=fast, sparks_roles=(role, ))


@task(aliases=('initial', ))
def runable(fast=False, upgrade=False):
    """ Ensure we can run the {web,dev}server: db+req+sync+migrate+static. """

    if not fast:
        execute_or_not(init_environment, sparks_roles=('__any__', ))

        execute_or_not(install_components, upgrade=upgrade,
                       sparks_roles=('__any__', ))

    # Push everything first.
    # Don't fail if local user doesn't have my aliases.
    local('git upa || git up || git pa || git push')

    if not is_local_environment():
        push_environment()  # already wraps execute_or_not()

        execute_or_not(git_update, sparks_roles=('web', 'worker',
                       'worker_low', 'worker_medium', 'worker_high'))

        if not is_production_environment():
            # fast or not, we must catch this one to
            # avoid source repository desynchronization.
            execute_or_not(push_translations, sparks_roles=('lang', ))

        execute_or_not(git_pull, sparks_roles=('web', 'worker',
                       'worker_low', 'worker_medium', 'worker_high'))

    execute_or_not(git_clean, sparks_roles=('web', 'worker',
                   'worker_low', 'worker_medium', 'worker_high'))

    requirements(fast=fast, upgrade=upgrade)  # already wraps execute_or_not()

    execute_or_not(compilemessages, sparks_roles=('web', 'worker',
                   'worker_low', 'worker_medium', 'worker_high'))

    execute_or_not(collectstatic, fast=fast, sparks_roles=('web', ))

    if not fast:
        execute_or_not(createdb, sparks_roles=('db', 'pg', ))

    execute_or_not(syncdb, sparks_roles=('db', 'pg', ))
    execute_or_not(migrate, sparks_roles=('db', 'pg', ))


@task(aliases=('fast', 'fastdeploy', ))
def fast_deploy():
    """ Deploy FAST! For templates / static changes only. """

    # not our execute_or_not(), here we want Fabric
    # to handle its classic execution model.
    execute(deploy, fast=True)


@task(default=True, aliases=('fulldeploy', 'full_deploy', ))
def deploy(fast=False, upgrade=False):
    """ Pull code, ensure runable, restart services. """

    # not our execute_or_not(), here we want Fabric
    # to handle its classic execution model.
    execute(runable, fast=fast, upgrade=upgrade)

    # not our execute_or_not(), here we want Fabric
    # to handle its classic execution model.
    execute(restart_services, fast=fast)
