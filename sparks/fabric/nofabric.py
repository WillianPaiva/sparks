# -*- coding: utf8 -*-
"""

Mini fabric-like runners and compatible API.

Used in :program:`1nstall` and when fabric is not available.

.. warning:: they're really MINIMAL, and won't work as well as fabric does.

"""

import subprocess
from ..foundations.classes import SimpleObject


def run(command, *a, **kw):

    output = SimpleObject()

    output.command = command

    try:
        #print '>> running', command
        output.output = subprocess.check_output(command,
                                                shell=kw.pop('shell', True),
                                                universal_newlines=True)

    except subprocess.CalledProcessError as e:
        output.output    = e.output
        output.failed    = True
        output.succeeded = False

    else:
        output.failed    = False
        output.succeeded = True

    return output


def sudo(command, *a, **kw):
    return run('sudo %s' % command, *a, **kw)


local = run