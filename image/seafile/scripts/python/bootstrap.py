#coding: UTF-8

"""
Bootstraping seafile server, letsencrypt (verification & cron job).
"""

import argparse
import os
from os.path import abspath, basename, exists, dirname, join, isdir
import shutil
import sys
import uuid
import time
import re

from utils import (
    call, get_conf, get_conf_bool, get_install_dir, loginfo,
    get_script, render_template, get_seafile_version, eprint,
    cert_has_valid_days, get_version_stamp_file, update_version_stamp,
    wait_for_mysql, wait_for_nginx, read_version_stamp
)


installdir = get_install_dir()
topdir = dirname(installdir)
shared_seafiledir = '/shared/seafile'
ssl_dir = '/shared/ssl'
generated_dir = '/bootstrap/generated'


def init_letsencrypt():
    loginfo('Preparing for letsencrypt ...')
    wait_for_nginx()

    if not exists(ssl_dir):
        os.mkdir(ssl_dir)

    domain = get_conf('SEAFILE_SERVER_HOSTNAME', 'seafile.example.com')
    context = {
        'ssl_dir': ssl_dir,
        'domain': domain,
    }
    render_template(
        '/templates/letsencrypt.cron.template',
        join(generated_dir, 'letsencrypt.cron'),
        context
    )

    ssl_crt = '/shared/ssl/{}.crt'.format(domain)
    if exists(ssl_crt):
        loginfo('Found existing cert file {}'.format(ssl_crt))
        if cert_has_valid_days(ssl_crt, 30):
            loginfo('Skip letsencrypt verification since we have a valid certificate')
            if exists(join(ssl_dir, 'letsencrypt')):
                # Create a crontab to auto renew the cert for letsencrypt.
                call('/scripts/auto_renew_crt.sh {0} {1}'.format(ssl_dir, domain))
            return

    loginfo('Starting letsencrypt verification')
    # Create a temporary nginx conf to start a server, which would accessed by letsencrypt
    context = {
        'https': False,
        'domain': domain,
    }
    if not os.path.isfile('/shared/nginx/conf/seafile.nginx.conf'):
        render_template('/templates/seafile.nginx.conf.template',
                        '/etc/nginx/sites-enabled/seafile.nginx.conf', context)

    call('nginx -s reload')
    time.sleep(2)

    call('/scripts/ssl.sh {0} {1}'.format(ssl_dir, domain))
    # if call('/scripts/ssl.sh {0} {1}'.format(ssl_dir, domain), check_call=False) != 0:
    #     eprint('Now waiting 1000s for postmortem')
    #     time.sleep(1000)
    #     sys.exit(1)

    call('/scripts/auto_renew_crt.sh {0} {1}'.format(ssl_dir, domain))
    # Create a crontab to auto renew the cert for letsencrypt.


def generate_local_nginx_conf():
    # Now create the final nginx configuratin
    domain = get_conf('SEAFILE_SERVER_HOSTNAME', 'seafile.example.com')
    context = {
        'https': is_https(),
        'behind_ssl_termination': behind_ssl_termination(),
        'enable_onlyoffice': use_onlyoffice(),
        'domain': domain,
        'enable_webdav': get_conf_bool('ENABLE_WEBDAV')
    }

    if not os.path.isfile('/shared/nginx/conf/seafile.nginx.conf'):
        render_template(
            '/templates/seafile.nginx.conf.template',
            '/etc/nginx/sites-enabled/seafile.nginx.conf',
            context
        )
        nginx_etc_file = '/etc/nginx/sites-enabled/seafile.nginx.conf'
        nginx_shared_file = '/shared/nginx/conf/seafile.nginx.conf'
        call('mv {0} {1} && ln -sf {1} {0}'.format(nginx_etc_file, nginx_shared_file))


def is_https():
    return get_conf_bool('SEAFILE_SERVER_LETSENCRYPT')


def behind_ssl_termination():
    return get_conf_bool('BEHIND_SSL_TERMINATION')


def use_onlyoffice():
    return get_conf_bool('ENABLE_ONLYOFFICE')


def init_seafile_server():
    version_stamp_file = get_version_stamp_file()
    if exists(join(shared_seafiledir, 'seafile-data')):
        if not exists(version_stamp_file):
            update_version_stamp(get_seafile_version())
        # sysbol link unlink after docker finish.
        latest_version_dir='/opt/seafile/seafile-server-latest'
        current_version_dir='/opt/seafile/' + get_conf('SEAFILE_SERVER', 'seafile-server') + '-' +  read_version_stamp()
        if not exists(latest_version_dir):
            call('ln -sf ' + current_version_dir + ' ' + latest_version_dir)
        loginfo('Skip running setup-seafile-mysql.py because there is existing seafile-data folder.')
        return

    loginfo('Now running setup-seafile-mysql.py in auto mode.')
    env = {
        'SERVER_NAME': 'seafile',
        'SERVER_IP': get_conf('SEAFILE_SERVER_HOSTNAME', 'seafile.example.com'),
        'MYSQL_USER': 'seafile',
        'MYSQL_USER_PASSWD': str(uuid.uuid4()),
        'MYSQL_USER_HOST': '%.%.%.%',
        'MYSQL_HOST': get_conf('DB_HOST','127.0.0.1'),
        # Default MariaDB root user has empty password and can only connect from localhost.
        'MYSQL_ROOT_PASSWD': get_conf('DB_ROOT_PASSWD', ''),
    }
    if get_conf_bool('USE_EXISTING_DB'):
        env['MYSQL_USER'] = get_conf('DB_USER', 'seafile')
        env['MYSQL_USER_PASSWD'] = get_conf('DB_USER_PASSWD', str(uuid.uuid4()))
        env['MYSQL_USER_HOST'] = get_conf('DB_USER_HOST', '%.%.%.%')
        env['CCNET_DB'] = get_conf('CCNET_DB', 'ccnet_db')
        env['SEAFILE_DB'] = get_conf('SEAFILE_DB', 'seafile_db')
        env['SEAHUB_DB'] = get_conf('SEAHUB_DB', 'seahub_db')

    # Change the script to allow mysql root password to be empty
    # call('''sed -i -e 's/if not mysql_root_passwd/if not mysql_root_passwd and "MYSQL_ROOT_PASSWD" not in os.environ/g' {}'''
    #     .format(get_script('setup-seafile-mysql.py')))

    # Change the script to disable check MYSQL_USER_HOST
    call('''sed -i -e '/def validate_mysql_user_host(self, host)/a \ \ \ \ \ \ \ \ return host' {}'''
        .format(get_script('setup-seafile-mysql.py')))

    call('''sed -i -e '/def validate_mysql_host(self, host)/a \ \ \ \ \ \ \ \ return host' {}'''
        .format(get_script('setup-seafile-mysql.py')))

    setup_script = get_script('setup-seafile-mysql.sh')
    call('{} auto -n seafile'.format(setup_script), env=env)

    domain = get_conf('SEAFILE_SERVER_HOSTNAME', 'seafile.example.com')
    proto = 'https' if is_https() or behind_ssl_termination() else 'http'
    with open(join(topdir, 'conf', 'seahub_settings.py'), 'a+') as fp:
        fp.write('\n')
        fp.write("""CACHES = {
    'default': {
        'BACKEND': 'django_pylibmc.memcached.PyLibMCCache',
        'LOCATION': 'memcached:11211',
    },
    'locmem': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    },
}
COMPRESS_CACHE_BACKEND = 'locmem'""")
        fp.write('\n')
        fp.write("TIME_ZONE = '{time_zone}'".format(time_zone=os.getenv('TIME_ZONE',default='Etc/UTC')))
        fp.write('\n')
        fp.write('FILE_SERVER_ROOT = "{proto}://{domain}/seafhttp"'.format(proto=proto, domain=domain))
        fp.write('\n')
        if get_conf_bool('ENABLE_ONLYOFFICE'):
            verify_oo_cert = "True" if is_https() or behind_ssl_termination() else "False"
            fp.write('\n')
            fp.write("ENABLE_ONLYOFFICE = True")
            fp.write('\n')
            fp.write('VERIFY_ONLYOFFICE_CERTIFICATE = {verify}'.format(verify=verify_oo_cert))
            fp.write('\n')
            fp.write('ONLYOFFICE_APIJS_URL = "{proto}://{domain}/onlyofficeds/web-apps/apps/api/documents/api.js"'.format(proto=proto, domain=domain))
            fp.write('\n')
            fp.write('ONLYOFFICE_FILE_EXTENSION = {ext}'.format(ext=get_conf("ONLYOFFICE_FILE_EXTENSION", 
                "('doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'odt', 'fodt', 'odp', 'fodp', 'ods', 'fods')")))
            fp.write('\n')
            fp.write('ONLYOFFICE_EDIT_FILE_EXTENSION = {ext}'.format(ext=get_conf("ONLYOFFICE_EDIT_FILE_EXTENSION", 
                "('docx', 'pptx', 'xlsx')")))
            fp.write('\n')


    # By default ccnet-server binds to the unix socket file
    # "/opt/seafile/ccnet/ccnet.sock", but /opt/seafile/ccnet/ is a mounted
    # volume from the docker host, and on windows and some linux environment
    # it's not possible to create unix sockets in an external-mounted
    # directories. So we change the unix socket file path to
    # "/opt/seafile/ccnet.sock" to avoid this problem.
    with open(join(topdir, 'conf', 'ccnet.conf'), 'a+') as fp:
        fp.write('\n')
        fp.write('[Client]\n')
        fp.write('UNIX_SOCKET = /opt/seafile/ccnet.sock\n')
        fp.write('\n')
        if get_conf_bool('ENABLE_LDAP'):
            fp.write('\n')
            fp.write('[LDAP]\n')
            fp.write('\n')
            fp.write('HOST = {host}'.format(host=get_conf("LDAP_HOST", "ldap://localhost")))
            fp.write('\n')
            fp.write('BASE = {base}'.format(base=get_conf("LDAP_BASE", "ou=users,dc=example,dc=com")))
            fp.write('\n')
            fp.write('USER_DN = {userdn}'.format(userdn=get_conf("LDAP_USER_DN", "cn=admin,dc=example,dc=com")))
            fp.write('\n')
            fp.write('PASSWORD = {passwd}'.format(passwd=get_conf("LDAP_PASSWORD", "secret")))
            fp.write('\n')
            fp.write('LOGIN_ATTR = {loginattr}'.format(loginattr=get_conf("LDAP_LOGIN_ATTR", "mail")))
            fp.write('\n')
            if get_conf("LDAP_FILTER", "") != "":
                fp.write('FILTER = {filter}'.format(filter=get_conf("LDAP_FILTER", "")))
                fp.write('\n')

    with open(join(topdir, 'conf', 'ccnet.conf'), "r") as fp:
        ccnet_conf_lines = fp.readlines()
    with open(join(topdir, 'conf', 'ccnet.conf'), "w") as fp:
        for line in ccnet_conf_lines:
            fp.write(re.sub(r"^SERVICE_URL = .*", "SERVICE_URL = {proto}://{domain}", line).format(proto=proto, domain=domain))

    # Setup seafdav
    if os.path.exists(join(topdir, 'conf', 'seafdav.conf')):
        with open(join(topdir, 'conf', 'seafdav.conf'), "r") as fp:
            seafdav_conf_lines = fp.readlines()
        with open(join(topdir, 'conf', 'seafdav.conf'), "w") as fp:
            seafdav_enabled = "true" if get_conf_bool('ENABLE_WEBDAV') else "false"
            for line in seafdav_conf_lines:
                if line.startswith("enabled"):
                    fp.write("enabled = {seafdav}\n".format(seafdav=seafdav_enabled))
                elif line.startswith("share_name"):
                    fp.write("share_name = /seafdav\n")
                else:
                    fp.write(line)

    # After the setup script creates all the files inside the
    # container, we need to move them to the shared volume
    #
    # e.g move "/opt/seafile/seafile-data" to "/shared/seafile/seafile-data"
    files_to_copy = ['conf', 'ccnet', 'seafile-data', 'seahub-data', 'pro-data']
    for fn in files_to_copy:
        src = join(topdir, fn)
        dst = join(shared_seafiledir, fn)
        if not exists(dst) and exists(src):
            shutil.move(src, shared_seafiledir)
            call('ln -sf ' + join(shared_seafiledir, fn) + ' ' + src)

    loginfo('Updating version stamp')
    update_version_stamp(get_seafile_version())
