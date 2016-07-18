#!/usr/local/bin/python
from middlewared.client import Client
from middlewared.client.utils import Struct

import os
import pwd
import re
import sys
import socket
import string
import tdb
import tempfile
import time
import logging
from dns import resolver

sys.path.extend([
    '/usr/local/www',
    '/usr/local/www/freenasUI'
])

os.environ["DJANGO_SETTINGS_MODULE"] = "freenasUI.settings"

# Make sure to load all modules
from django.db.models.loading import cache
cache.get_apps()

from freenasUI.common.pipesubr import pipeopen
from freenasUI.common.log import log_traceback
from freenasUI.common.samba import Samba4
from freenasUI.choices import IPChoices
from freenasUI.directoryservice.models import (
    IDMAP_TYPE_AD,
    IDMAP_TYPE_ADEX,
    IDMAP_TYPE_AUTORID,
    IDMAP_TYPE_HASH,
    IDMAP_TYPE_LDAP,
    IDMAP_TYPE_NSS,
    IDMAP_TYPE_RFC2307,
    IDMAP_TYPE_RID,
    IDMAP_TYPE_TDB,
    IDMAP_TYPE_TDB2,
    DS_TYPE_CIFS,
    idmap_to_enum
)


log = logging.getLogger('generate_smb4_conf')


def debug_SID(str):
    if str:
        print >> sys.stderr, "XXX: %s" % str
    p = pipeopen("/usr/local/bin/net -d 0 getlocalsid")
    out, _ = p.communicate()
    if out:
        print >> sys.stderr, "XXX: %s" % out


def smb4_get_system_SID():
    SID = None

    p = pipeopen("/usr/local/bin/net -d 0 getlocalsid")
    net_out = p.communicate()
    if p.returncode != 0:
        return None
    if not net_out:
        return None

    net_out = net_out[0]

    parts = net_out.split()
    try:
        SID = parts[5]
    except Exception as e:
        log.debug(
            'The following exception occured while trying to obtain system SID: {0}'.format(e)
        )
        log_traceback(log=log)
        SID = None

    return SID


def smb4_get_database_SID(client):
    SID = None

    try:
        cifs = Struct(client.call('datastore.query', 'services.cifs', None, {'get': True}))
        if cifs:
            SID = cifs.cifs_SID
    except Exception as e:
        log.debug(
            'The following exception occured while trying to obtain database SID: {0}'.format(e)
        )
        log_traceback(log=log)
        SID = None

    return SID


def smb4_set_system_SID(SID):
    if not SID:
        return False

    p = pipeopen("/usr/local/bin/net -d 0 setlocalsid %s" % SID)
    net_out = p.communicate()
    if p.returncode != 0:
        log.error('Failed to setlocalsid with the following error: {0}'.format(net_out[1]))
        return False
    if not net_out:
        return False

    return True


def smb4_set_database_SID(client, SID):
    ret = False
    if not SID:
        return ret

    try:
        cifs = Struct(client.call('datastore.query', 'services.cifs', None, {'get': True}))
        cifs.cifs_SID = SID
        cifs.save()
        ret = True

    except Exception as e:
        log.debug(
            'The following exception occured while trying to set database SID: {0}'.format(e)
        )
        log_traceback(log=log)
        ret = False

    return ret


def smb4_set_SID(client):
    database_SID = smb4_get_database_SID(client)
    system_SID = smb4_get_system_SID()

    if database_SID:
        if not system_SID:
            if not smb4_set_system_SID(database_SID):
                print >> sys.stderr, "Unable to set SID to %s" % database_SID
        else:
            if database_SID != system_SID:
                if not smb4_set_system_SID(database_SID):
                    print >> sys.stderr, ("Unable to set SID to "
                                          "%s" % database_SID)

    else:
        if not system_SID:
            print >> sys.stderr, ("Unable to figure out SID, things are "
                                  "seriously jacked!")

        if not smb4_set_system_SID(system_SID):
            print >> sys.stderr, "Unable to set SID to %s" % system_SID
        else:
            smb4_set_database_SID(client, system_SID)


def smb4_ldap_enabled(client):
    ret = False

    if client.call('notifier.common', 'system', 'ldap_enabled') and client.call('notifier.common', 'system', 'ldap_has_samba_schema'):
        ret = True

    return ret


def config_share_for_nfs4(share):
    confset1(share, "nfs4:mode = special")
    confset1(share, "nfs4:acedup = merge")
    confset1(share, "nfs4:chown = true")


def config_share_for_zfs(share):
    confset1(share, "zfsacl:acesort = dontcare")


#
# ticket: # 16325
# aio_pthread needs to be last
# fruit needs to be before streams_xattr, streams_xattr is required
# for fruit, and if catia and fruit are used, catia comes before fruit
#
def order_vfs_objects(vfs_objects):
    vfs_objects_ordered = []
    for obj in vfs_objects:
        if obj not in ('aio_pthread', 'catia', 'fruit', 'streams_xattr'):
            vfs_objects_ordered.append(obj)

    if 'fruit' in vfs_objects:
        if 'catia' in vfs_objects:
            vfs_objects_ordered.append('catia')
        vfs_objects_ordered.append('fruit')
        vfs_objects_ordered.append('streams_xattr')
    if 'aio_pthread' in vfs_objects:
        vfs_objects_ordered.append('aio_pthread')

    return vfs_objects_ordered


def config_share_for_vfs_objects(share, vfs_objects):
    if vfs_objects:
        vfs_objects = order_vfs_objects(vfs_objects)
        confset2(share, "vfs objects = %s", ' '.join(vfs_objects).encode('utf8'))


def extend_vfs_objects_for_zfs(path, vfs_objects):
    # Temporarily remove zfs_space until #16573 is solved
    if is_within_zfs(path):
        vfs_objects.extend([
            'zfsacl',
        ])


def is_within_zfs(mountpoint):
    try:
        st = os.stat(mountpoint)
    except:
        return False

    share_dev = st.st_dev

    p = pipeopen("mount")

    mount_out = p.communicate()
    if p.returncode != 0:
        return False
    if mount_out:
        mount_out = mount_out[0]

    zfs_regex = re.compile("^(.*) on (/.*) \(zfs, .*\)$")

    # The reversed is important as we would like the code to use
    # the most specific (and therefore relevant) mount point.
    for line in reversed(mount_out.split('\n')):
        match = zfs_regex.match(line.strip())
        if not match:
            continue

        mp = match.group(2)

        try:
            st = os.stat(mp)
        except:
            continue

        if st.st_dev == share_dev:
            return True

    return False


def get_sysctl(name):
    p = pipeopen("/sbin/sysctl -n '%s'" % name)
    out = p.communicate()
    if p.returncode != 0:
        return None
    try:
        out = out[0].strip()
    except:
        pass
    return out


def get_server_services():
    server_services = [
        'rpc', 'nbt', 'wrepl', 'ldap', 'cldap', 'kdc', 'drepl', 'winbind',
        'ntp_signd', 'kcc', 'dnsupdate', 'dns', 'smb'
    ]
    return server_services


def get_dcerpc_endpoint_servers():
    dcerpc_endpoint_servers = [
        'epmapper', 'wkssvc', 'rpcecho', 'samr', 'netlogon', 'lsarpc',
        'spoolss', 'drsuapi', 'dssetup', 'unixinfo', 'browser', 'eventlog6',
        'backupkey', 'dnsserver', 'winreg', 'srvsvc'
    ]
    return dcerpc_endpoint_servers


def get_server_role(client):
    role = "standalone"
    if client.call('notifier.common', 'system', 'nt4_enabled') or client.call('notifier.common', 'system', 'activedirectory_enabled') or smb4_ldap_enabled(client):
        role = "member"

    if client.call('notifier.common', 'system', 'domaincontroller_enabled'):
        try:
            role = client.call('datastore.query', 'services.DomainController', None, {'get': True})['dc_role']
        except:
            pass

    return role


def confset1(conf, line, space=4):
    if line:
        conf.append(' ' * space + line)


def confset2(conf, line, var, space=4):
    if line and var:
        line = ' ' * space + line
        conf.append(line % var)


def configure_idmap_ad(smb4_conf, idmap, domain):
    confset1(smb4_conf, "idmap config %s: backend = %s" % (
        domain,
        idmap.idmap_backend_name
    ))
    confset1(smb4_conf, "idmap config %s: range = %d-%d" % (
        domain,
        idmap.idmap_ad_range_low,
        idmap.idmap_ad_range_high
    ))
    confset1(smb4_conf, "idmap config %s: schema mode = %s" % (
        domain,
        idmap.idmap_ad_schema_mode
    ))


def configure_idmap_adex(smb4_conf, idmap, domain):
    confset1(smb4_conf, "idmap backend = adex")
    confset1(smb4_conf, "idmap uid = %d-%d" % (
        domain,
        idmap.idmap_adex_range_low,
        idmap.idmap_adex_range_high
    ))
    confset1(smb4_conf, "idmap gid = %d-%d" % (
        domain,
        idmap.idmap_adex_range_low,
        idmap.idmap_adex_range_high
    ))


def configure_idmap_autorid(smb4_conf, idmap, domain):
    confset1(smb4_conf, "idmap config %s: backend = %s" % (
        domain,
        idmap.idmap_backend_name
    ))
    confset1(smb4_conf, "idmap config %s: range = %d-%d" % (
        domain,
        idmap.idmap_autorid_range_low,
        idmap.idmap_autorid_range_high
    ))
    confset1(smb4_conf, "idmap config %s: rangesize = %d" % (
        domain,
        idmap.idmap_autorid_rangesize
    ))
    confset1(smb4_conf, "idmap config %s: read only = %s" % (
        domain,
        "yes" if idmap.idmap_autorid_readonly else "no"
    ))
    confset1(smb4_conf, "idmap config %s: ignore builtin = %s" % (
        domain,
        "yes" if idmap.idmap_autorid_ignore_builtin else "no"
    ))


def configure_idmap_hash(smb4_conf, idmap, domain):
    confset1(smb4_conf, "idmap config %s: backend = %s" % (
        domain,
        idmap.idmap_backend_name
    ))
    confset1(smb4_conf, "idmap config %s: range = %d-%d" % (
        domain,
        idmap.idmap_hash_range_low,
        idmap.idmap_hash_range_high
    ))
    confset1(smb4_conf, "idmap_hash: name_map = %s" %
             idmap.idmap_hash_range_name_map)


def configure_idmap_ldap(smb4_conf, idmap, domain):
    confset1(smb4_conf, "idmap config %s: backend = %s" % (
        domain,
        idmap.idmap_backend_name
    ))
    confset1(smb4_conf, "idmap config %s: range = %d-%d" % (
        domain,
        idmap.idmap_ldap_range_low,
        idmap.idmap_ldap_range_high
    ))
    if idmap.idmap_ldap_ldap_base_dn:
        confset1(smb4_conf, "idmap config %s: ldap base dn = %s" % (
            domain,
            idmap.idmap_ldap_ldap_base_dn
        ))
    if idmap.idmap_ldap_ldap_user_dn:
        confset1(smb4_conf, "idmap config %s: ldap user dn = %s" % (
            domain,
            idmap.idmap_ldap_ldap_user_dn
        ))
    if idmap.idmap_ldap_ldap_url:
        confset1(smb4_conf, "idmap config %s: ldap url = %s" % (
            domain,
            idmap.idmap_ldap_ldap_url
        ))


def configure_idmap_nss(smb4_conf, idmap, domain):
    confset1(smb4_conf, "idmap config %s: backend = %s" % (
        domain,
        idmap.idmap_backend_name
    ))
    confset1(smb4_conf, "idmap config %s: range = %d-%d" % (
        domain,
        idmap.idmap_nss_range_low,
        idmap.idmap_nss_range_high
    ))


def configure_idmap_rfc2307(smb4_conf, idmap, domain):
    confset1(smb4_conf, "idmap config %s: backend = %s" % (
        domain,
        idmap.idmap_backend_name
    ))
    confset1(smb4_conf, "idmap config %s: range = %d-%d" % (
        domain,
        idmap.idmap_rfc2307_range_low,
        idmap.idmap_rfc2307_range_high
    ))
    confset1(smb4_conf, "idmap config %s: ldap_server = %s" % (
        domain,
        idmap.idmap_rfc2307_ldap_server
    ))
    confset1(smb4_conf, "idmap config %s: bind_path_user = %s" % (
        domain,
        idmap.idmap_rfc2307_bind_path_user
    ))
    confset1(smb4_conf, "idmap config %s: bind_path_group = %s" % (
        domain,
        idmap.idmap_rfc2307_bind_path_group
    ))
    confset1(smb4_conf, "idmap config %s: user_cn = %s" % (
        domain,
        "yes" if idmap.idmap_rfc2307_user_cn else "no"
    ))
    confset1(smb4_conf, "idmap config %s: cn_realm = %s" % (
        domain,
        "yes" if idmap.idmap_rfc2307_cn_realm else "no"
    ))
    if idmap.idmap_rfc2307_ldap_domain:
        confset1(smb4_conf, "idmap config %s: ldap_domain = %s" % (
            domain,
            idmap.idmap_rfc2307_ldap_domain
        ))
    if idmap.idmap_rfc2307_ldap_url:
        confset1(smb4_conf, "idmap config %s: ldap_url = %s" % (
            domain,
            idmap.idmap_rfc2307_ldap_url
        ))
    if idmap.idmap_rfc2307_ldap_user_dn:
        confset1(smb4_conf, "idmap config %s: ldap_user_dn = %s" % (
            domain,
            idmap.idmap_rfc2307_ldap_user_dn
        ))
    if idmap.idmap_rfc2307_ldap_realm:
        confset1(smb4_conf, "idmap config %s: ldap_realm = %s" % (
            domain,
            idmap.idmap_rfc2307_ldap_realm
        ))


def idmap_backend_rfc2307(client):
    try:
        ad = Struct(client.call('datastore.query', 'directoryservice.ActiveDirectory', None, {'get': True}))
    except:
        return False

    return (idmap_to_enum(ad.ad_idmap_backend) == IDMAP_TYPE_RFC2307)


def set_idmap_rfc2307_secret(client):
    try:
        ad = Struct(client.call('datastore.query', 'directoryservice.ActiveDirectory', None, {'get': True}))
    except:
        return False

    domain = None
    # FIXME: ad ds_type, extend model
    idmap = Struct(client.call('notifier.ds_get_idmap_object', ad.ds_type, ad.id, ad.ad_idmap_backend))

    try:
        fad = Struct(client.call('notifier.directoryservice', 'AD'))
        domain = fad.netbiosname.upper()
    except:
        return False

    args = [
        "/usr/local/bin/net",
        "-d 0",
        "idmap",
        "secret"
    ]

    net_cmd = "%s '%s' '%s'" % (
        string.join(args, ' '),
        domain,
        idmap.idmap_rfc2307_ldap_user_dn_password
    )

    p = pipeopen(net_cmd, quiet=True)
    net_out = p.communicate()
    if net_out and net_out[0]:
        for line in net_out[0].split('\n'):
            if not line:
                continue
            print line

    ret = True
    if p.returncode != 0:
        print >> sys.stderr, "Failed to set idmap secret!"
        ret = False

    return ret


def configure_idmap_rid(smb4_conf, idmap, domain):
    confset1(smb4_conf, "idmap config %s: backend = %s" % (
        domain,
        idmap.idmap_backend_name
    ))
    confset1(smb4_conf, "idmap config %s: range = %d-%d" % (
        domain,
        idmap.idmap_rid_range_low,
        idmap.idmap_rid_range_high
    ))


def configure_idmap_tdb(smb4_conf, idmap, domain):
    confset1(smb4_conf, "idmap config %s: backend = %s" % (
        domain,
        idmap.idmap_backend_name
    ))
    confset1(smb4_conf, "idmap config %s: range = %d-%d" % (
        domain,
        idmap.idmap_tdb_range_low,
        idmap.idmap_tdb_range_high
    ))


def configure_idmap_tdb2(smb4_conf, idmap, domain):
    confset1(smb4_conf, "idmap config %s: backend = %s" % (
        domain,
        idmap.idmap_backend_name
    ))
    confset1(smb4_conf, "idmap config %s: range = %d-%d" % (
        domain,
        idmap.idmap_tdb2_range_low,
        idmap.idmap_tdb2_range_high
    ))
    confset2(smb4_conf, "idmap config %s: script = %s" % (
        domain,
        idmap.idmap_tdb2_script
    ))


IDMAP_FUNCTIONS = {
    IDMAP_TYPE_AD: configure_idmap_ad,
    IDMAP_TYPE_ADEX: configure_idmap_ad,
    IDMAP_TYPE_AUTORID: configure_idmap_autorid,
    IDMAP_TYPE_HASH: configure_idmap_hash,
    IDMAP_TYPE_LDAP: configure_idmap_ldap,
    IDMAP_TYPE_NSS: configure_idmap_nss,
    IDMAP_TYPE_RFC2307: configure_idmap_rfc2307,
    IDMAP_TYPE_RID: configure_idmap_rid,
    IDMAP_TYPE_TDB: configure_idmap_tdb,
    IDMAP_TYPE_TDB2: configure_idmap_tdb2
}


def configure_idmap_backend(smb4_conf, idmap, domain):
    if not domain:
        domain = "*"

    try:
        IDMAP_FUNCTIONS[idmap.idmap_backend_type](smb4_conf, idmap, domain)
    except:
        pass


def add_nt4_conf(client, smb4_conf):
    # TODO: These are unused, will they be at some point?
    # rid_range_start = 20000
    # rid_range_end = 20000000

    try:
        nt4 = Struct(client.call('datastore.query', 'directoryservice.nt4', None, {'get': True}))
    except:
        return

    dc_ip = None
    try:
        answers = resolver.query(nt4.nt4_dcname, 'A')
        dc_ip = answers[0]

    except Exception as e:
        log.debug(
            "resolver query for {0}'s A record failed with {1}".format(nt4.nt4_dcname, e)
        )
        log_traceback(log=log)
        dc_ip = nt4.nt4_dcname

    nt4_workgroup = nt4.nt4_workgroup.upper()

    with open("/usr/local/etc/lmhosts", "w") as f:
        f.write("%s\t%s\n" % (dc_ip, nt4.nt4_dcname.upper()))

    confset2(smb4_conf, "workgroup = %s", nt4_workgroup)

    confset1(smb4_conf, "security = domain")
    confset1(smb4_conf, "password server = *")

    # FIXME: no ds_type, extend model
    idmap = client.call('notifier.ds_get_idmap_object', nt4.ds_type, nt4.id, nt4.nt4_idmap_backend)
    configure_idmap_backend(smb4_conf, idmap, nt4_workgroup)

    confset1(smb4_conf, "winbind cache time = 7200")
    confset1(smb4_conf, "winbind offline logon = yes")
    confset1(smb4_conf, "winbind enum users = yes")
    confset1(smb4_conf, "winbind enum groups = yes")
    confset1(smb4_conf, "winbind nested groups = yes")
    confset2(
        smb4_conf, "winbind use default domain = %s", "yes" if nt4.nt4_use_default_domain else "no"
    )

    confset1(smb4_conf, "template shell = /bin/sh")

    confset1(smb4_conf, "local master = no")
    confset1(smb4_conf, "domain master = no")
    confset1(smb4_conf, "preferred master = no")


def set_ldap_password(client):
    try:
        ldap = Struct(client.call('datastore.query', 'directoryservice.LDAP', None, {'get': True}))
    except:
        return

    if ldap.ldap_bindpw:
        p = pipeopen("/usr/local/bin/smbpasswd -w '%s'" % (
            ldap.ldap_bindpw,
        ), quiet=True)
        out = p.communicate()
        if out and out[1]:
            for line in out[1].split('\n'):
                if not line:
                    continue
                print line


def add_ldap_conf(client, smb4_conf):
    try:
        ldap = Struct(client.call('datastore.query', 'directoryservice.LDAP', None, {'get': True}))
        cifs = Struct(client.call('datastore.query', 'services.CIFS', None, {'get': True}))
    except:
        return

    confset1(smb4_conf, "security = user")

    confset1(
        smb4_conf,
        "passdb backend = ldapsam:%s://%s" % (
            "ldaps" if ldap.ldap_ssl == 'on' else "ldap",
            ldap.ldap_hostname
        )
    )

    ldap_workgroup = cifs.cifs_srv_workgroup.upper()

    confset2(smb4_conf, "ldap admin dn = %s", ldap.ldap_binddn)
    confset2(smb4_conf, "ldap suffix = %s", ldap.ldap_basedn)
    confset2(smb4_conf, "ldap user suffix = %s", ldap.ldap_usersuffix)
    confset2(smb4_conf, "ldap group suffix = %s", ldap.ldap_groupsuffix)
    confset2(smb4_conf, "ldap machine suffix = %s", ldap.ldap_machinesuffix)
    confset2(
        smb4_conf,
        "ldap ssl = %s",
        "start tls" if (ldap.ldap_ssl == 'start_tls') else 'off'
    )

    confset1(smb4_conf, "ldap replication sleep = 1000")
    confset1(smb4_conf, "ldap passwd sync = yes")
    confset1(smb4_conf, "ldapsam:trusted = yes")

    confset2(smb4_conf, "workgroup = %s", ldap_workgroup)
    confset1(smb4_conf, "domain logons = yes")

    # FIXME: no ds_type, extend or use static
    idmap = client.call('notifier.ds_get_idmap_object', ldap.ds_type, ldap.id, ldap.ldap_idmap_backend)
    configure_idmap_backend(smb4_conf, idmap, ldap_workgroup)


def add_activedirectory_conf(client, smb4_conf):
    try:
        ad = Struct(client.call('datastore.query', 'directoryservice.ActiveDirectory', None, {'get': True}))
    except:
        return

    cachedir = "/var/tmp/.cache/.samba"

    try:
        os.makedirs(cachedir)
        os.chmod(cachedir, 0755)
    except:
        pass

    ad_workgroup = None
    try:
        fad = Struct(client.call('notifier.directoryservice', 'AD'))
        ad_workgroup = fad.netbiosname.upper()
    except:
        return

    confset2(smb4_conf, "workgroup = %s", ad_workgroup)
    confset2(smb4_conf, "realm = %s", ad.ad_domainname.upper())
    confset1(smb4_conf, "security = ADS")
    confset1(smb4_conf, "client use spnego = yes")
    confset2(smb4_conf, "cache directory = %s", cachedir)

    confset1(smb4_conf, "local master = no")
    confset1(smb4_conf, "domain master = no")
    confset1(smb4_conf, "preferred master = no")

    confset2(smb4_conf, "ads dns update = %s",
             "yes" if ad.ad_allow_dns_updates else "no")

    confset1(smb4_conf, "winbind cache time = 7200")
    confset1(smb4_conf, "winbind offline logon = yes")
    confset1(smb4_conf, "winbind enum users = yes")
    confset1(smb4_conf, "winbind enum groups = yes")
    confset1(smb4_conf, "winbind nested groups = yes")
    confset2(smb4_conf, "winbind use default domain = %s",
             "yes" if ad.ad_use_default_domain else "no")
    confset1(smb4_conf, "winbind refresh tickets = yes")

    if ad.ad_nss_info:
        confset2(smb4_conf, "winbind nss info = %s", ad.ad_nss_info)

    # FIXME: no ds_type, use static or extend model
    idmap = client.call('notifier.ds_get_idmap_object', ad.ds_type, ad.id, ad.ad_idmap_backend)
    configure_idmap_backend(smb4_conf, idmap, ad_workgroup)

    confset2(smb4_conf, "allow trusted domains = %s",
             "yes" if ad.ad_allow_trusted_doms else "no")

    confset2(smb4_conf, "client ldap sasl wrapping = %s",
             ad.ad_ldap_sasl_wrapping)

    confset1(smb4_conf, "template shell = /bin/sh")
    confset2(smb4_conf, "template homedir = %s",
             "/home/%D/%U" if not ad.ad_use_default_domain else "/home/%U")


def add_domaincontroller_conf(client, smb4_conf):
    try:
        dc = Struct(client.call('datastore.query', 'services.DomainController', None, {'get': True}))
        cifs = Struct(client.call('cifs.config'))
    except:
        return

    # server_services = get_server_services()
    # dcerpc_endpoint_servers = get_dcerpc_endpoint_servers()

    confset2(smb4_conf, "netbios name = %s", cifs.netbiosname.upper())
    if cifs.cifs_srv_netbiosalias:
        confset2(smb4_conf, "netbios aliases = %s", cifs.cifs_srv_netbiosalias.upper())
    confset2(smb4_conf, "workgroup = %s", dc.dc_domain.upper())
    confset2(smb4_conf, "realm = %s", dc.dc_realm)
    confset2(smb4_conf, "dns forwarder = %s", dc.dc_dns_forwarder)
    confset1(smb4_conf, "idmap_ldb:use rfc2307 = yes")

    # confset2(smb4_conf, "server services = %s",
    #    string.join(server_services, ',').rstrip(','))
    # confset2(smb4_conf, "dcerpc endpoint servers = %s",
    #    string.join(dcerpc_endpoint_servers, ',').rstrip(','))

    ipv4_addrs = []
    if cifs.cifs_srv_bindip:
        for i in cifs.cifs_srv_bindip:
            try:
                socket.inet_aton(i)
                ipv4_addrs.append(i)
            except:
                pass

    else:
        interfaces = IPChoices(ipv6=False)
        for i in interfaces:
            try:
                socket.inet_aton(i[0])
                ipv4_addrs.append(i[0])
            except:
                pass

    with open("/usr/local/etc/lmhosts", "w") as f:
        for ipv4 in ipv4_addrs:
            f.write("%s\t%s\n" % (ipv4, dc.dc_domain.upper()))


def get_smb4_users(client):
    # FIXME: test query and support for OR
    return client.call('datastore.query', 'account.bsdusers', (
        'OR', [
            ('bsdusr_smbhash', '~', r'^.+:.+:[X]{32}:.+$'),
            ('bsdusr_smbhash', '~', r'^.+:.+:[A-F0-9]{32}:.+$'),
        ],
    ))


def get_disabled_users(client):
    # XXX: WTF moment, this method is not used
    disabled_users = []
    try:
        # FIXME: test query and support for OR
        users = client.call('datastore.query', 'account.bsdusers', (
            ('bsdusr_smbhash', '~', r'^.+:.+:XXXX.+$'),
            (
                'OR',
                ('bsdusr_locked', '=', True),
                ('bsdusr_password_disabled', '=', True),
            ),
        ))
        for u in users:
            disabled_users.append(u)

    except:
        disabled_users = []

    return disabled_users


def generate_smb4_tdb(client, smb4_tdb):
    try:
        users = get_smb4_users(client)
        for u in users:
            smb4_tdb.append(u['bsdusr_smbhash'])
    except:
        return


def generate_smb4_conf(client, smb4_conf, role):
    try:
        cifs = Struct(client.call('cifs.config'))
    except:
        return

    if not cifs.cifs_srv_guest:
        cifs.cifs_srv_guest = 'ftp'
    if not cifs.cifs_srv_filemask:
        cifs.cifs_srv_filemask = "0666"
    if not cifs.cifs_srv_dirmask:
        cifs.cifs_srv_dirmask = "0777"

    # standard stuff... should probably do this differently
    confset1(smb4_conf, "[global]", space=0)

    if os.path.exists("/usr/local/etc/smbusers"):
        confset1(smb4_conf, "username map = /usr/local/etc/smbusers")

    confset2(smb4_conf, "server min protocol = %s", cifs.cifs_srv_min_protocol)
    confset2(smb4_conf, "server max protocol = %s", cifs.cifs_srv_max_protocol)
    if cifs.cifs_srv_bindip:
        interfaces = []

        bindips = string.join(cifs.cifs_srv_bindip, ' ')
        if role != 'dc':
            bindips = "127.0.0.1 %s" % bindips

        bindips = bindips.split()
        for bindip in bindips:
            if not bindip:
                continue
            bindip = bindip.strip()
            iface = client.call('notifier.get_interface', bindip)
            if iface and client.call('notifier.is_carp_interface', iface):
                parent_iface = client.call('notifier.get_parent_interface', iface)
                if not parent_iface:
                    continue

                parent_iinfo = client.call('notifier.get_interface_info', parent_iface[0])
                if not parent_iinfo:
                    continue

                interfaces.append("%s/%s" % (bindip, parent_iface[2]))
            else:
                interfaces.append(bindip)

        if interfaces:
            confset2(smb4_conf, "interfaces = %s", string.join(interfaces))
        confset1(smb4_conf, "bind interfaces only = yes")

    confset1(smb4_conf, "encrypt passwords = yes")
    confset1(smb4_conf, "dns proxy = no")
    confset1(smb4_conf, "strict locking = no")
    confset1(smb4_conf, "oplocks = yes")
    confset1(smb4_conf, "deadtime = 15")
    confset1(smb4_conf, "max log size = 51200")

    confset2(smb4_conf, "max open files = %d",
             long(get_sysctl('kern.maxfilesperproc')) - 25)

    if cifs.cifs_srv_loglevel and cifs.cifs_srv_loglevel is not True:
        loglevel = cifs.cifs_srv_loglevel
    else:
        loglevel = "0"

    if cifs.cifs_srv_syslog:
        confset1(smb4_conf, "logging = syslog:%s" % loglevel)
    else:
        confset1(smb4_conf, "logging = file")

    confset1(smb4_conf, "load printers = no")
    confset1(smb4_conf, "printing = bsd")
    confset1(smb4_conf, "printcap name = /dev/null")
    confset1(smb4_conf, "disable spoolss = yes")
    confset1(smb4_conf, "getwd cache = yes")
    confset2(smb4_conf, "guest account = %s",
             cifs.cifs_srv_guest.encode('utf8'))
    confset1(smb4_conf, "map to guest = Bad User")
    confset2(smb4_conf, "obey pam restrictions = %s",
             "yes" if cifs.cifs_srv_obey_pam_restrictions else "no")
    confset1(smb4_conf, "directory name cache size = 0")
    confset1(smb4_conf, "kernel change notify = no")

    confset1(smb4_conf,
             "panic action = /usr/local/libexec/samba/samba-backtrace")
    confset1(smb4_conf, "nsupdate command = /usr/local/bin/samba-nsupdate -g")

    confset2(smb4_conf, "server string = %s", cifs.cifs_srv_description)
    confset1(smb4_conf, "ea support = yes")
    confset1(smb4_conf, "store dos attributes = yes")
    confset1(smb4_conf, "lm announce = yes")
    confset2(smb4_conf, "hostname lookups = %s",
             "yes" if cifs.cifs_srv_hostlookup else False)
    confset2(smb4_conf, "unix extensions = %s",
             "no" if not cifs.cifs_srv_unixext else False)
    confset2(smb4_conf, "time server = %s",
             "yes" if cifs.cifs_srv_timeserver else False)
    confset2(smb4_conf, "null passwords = %s",
             "yes" if cifs.cifs_srv_nullpw else False)
    confset2(smb4_conf, "acl allow execute always = %s",
             "true" if cifs.cifs_srv_allow_execute_always else "false")
    confset1(smb4_conf, "dos filemode = yes")
    confset2(smb4_conf, "multicast dns register = %s",
             "yes" if cifs.cifs_srv_zeroconf else "no")

    if not smb4_ldap_enabled():
        confset2(smb4_conf, "domain logons = %s",
                 "yes" if cifs.cifs_srv_domain_logons else "no")

    if (not client.call('notifier.common', 'system', 'nt4_enabled') and not client.call('notifier.common', 'system', 'activedirectory_enabled')):
        confset2(smb4_conf, "local master = %s",
                 "yes" if cifs.cifs_srv_localmaster else "no")

    idmap = client.call('notifier.ds_get_idmap_object', DS_TYPE_CIFS, cifs.id, 'tdb')
    configure_idmap_backend(smb4_conf, idmap, None)

    if role == 'auto':
        confset1(smb4_conf, "server role = auto")

    elif role == 'classic':
        confset1(smb4_conf, "server role = classic primary domain controller")

    elif role == 'netbios':
        confset1(smb4_conf, "server role = netbios backup domain controller")

    elif role == 'dc':
        confset1(smb4_conf, "server role = active directory domain controller")
        add_domaincontroller_conf(client, smb4_conf)

    elif role == 'member':
        confset1(smb4_conf, "server role = member server")

        if client.call('notifier.common', 'system', 'nt4_enabled'):
            add_nt4_conf(client, smb4_conf)

        elif smb4_ldap_enabled():
            add_ldap_conf(client, smb4_conf)

        elif client.call('notifier.common', 'system', 'activedirectory_enabled'):
            add_activedirectory_conf(client, smb4_conf)

        confset2(smb4_conf, "netbios name = %s", cifs.netbiosname.upper())
        if cifs.cifs_srv_netbiosalias:
            confset2(smb4_conf, "netbios aliases = %s", cifs.cifs_srv_netbiosalias.upper())

    elif role == 'standalone':
        confset1(smb4_conf, "server role = standalone")
        confset2(smb4_conf, "netbios name = %s", cifs.netbiosname.upper())
        if cifs.cifs_srv_netbiosalias:
            confset2(smb4_conf, "netbios aliases = %s", cifs.cifs_srv_netbiosalias.upper())
        confset2(smb4_conf, "workgroup = %s", cifs.cifs_srv_workgroup.upper())
        confset1(smb4_conf, "security = user")

    if role != 'dc':
        confset1(smb4_conf, "pid directory = /var/run/samba")

    confset2(smb4_conf, "create mask = %s", cifs.cifs_srv_filemask)
    confset2(smb4_conf, "directory mask = %s", cifs.cifs_srv_dirmask)
    confset1(smb4_conf, "client ntlmv2 auth = yes")
    confset2(smb4_conf, "dos charset = %s", cifs.cifs_srv_doscharset)
    confset2(smb4_conf, "unix charset = %s", cifs.cifs_srv_unixcharset)

    if cifs.cifs_srv_loglevel and cifs.cifs_srv_loglevel is not True:
        confset2(smb4_conf, "log level = %s", cifs.cifs_srv_loglevel)

    smb_options = cifs.cifs_srv_smb_options.encode('utf-8')
    smb_options = smb_options.strip()
    for line in smb_options.split('\n'):
        line = line.strip()
        if not line:
            continue
        confset1(smb4_conf, line)


def generate_smb4_shares(client, smb4_shares):
    try:
        shares = client.call('datastore.query', 'sharing.CIFS_Share')
    except:
        log.warn('Failed to retrieve cifs shares', exc_info=True)
        return

    if len(shares) == 0:
        return

    for share in shares:
        share = Struct(share)
        if (not share.cifs_home and
                not os.path.isdir(share.cifs_path.encode('utf8'))):
            continue

        confset1(smb4_shares, "\n")
        if share.cifs_home:
            confset1(smb4_shares, "[homes]", space=0)

            valid_users_path = "%U"
            valid_users = "%U"

            if client.call('notifier.common', 'system', 'activedirectory_enabled'):
                try:
                    ad = Struct(client.call('datastore.query', 'directoryservice.ActiveDirectory', None, {'get': True}))
                    if not ad.ad_use_default_domain:
                        valid_users_path = "%D/%U"
                        valid_users = "%D\%U"
                except:
                    pass

            confset2(smb4_shares, "valid users = %s", valid_users)

            if share.cifs_path:
                cifs_homedir_path = (u"%s/%s" %
                                     (share.cifs_path, valid_users_path))
                confset2(smb4_shares, "path = %s",
                         cifs_homedir_path.encode('utf8'))
            if share.cifs_comment:
                confset2(smb4_shares,
                         "comment = %s", share.cifs_comment.encode('utf8'))
            else:
                confset1(smb4_shares, "comment = Home Directories")
        else:
            confset2(smb4_shares, "[%s]",
                     share.cifs_name.encode('utf8'), space=0)
            confset2(smb4_shares, "path = %s", share.cifs_path.encode('utf8'))
            confset2(smb4_shares, "comment = %s",
                     share.cifs_comment.encode('utf8'))
        confset1(smb4_shares, "printable = no")
        confset1(smb4_shares, "veto files = /.snapshot/.windows/.mac/.zfs/")
        confset2(smb4_shares, "writeable = %s",
                 "no" if share.cifs_ro else "yes")
        confset2(smb4_shares, "browseable = %s",
                 "yes" if share.cifs_browsable else "no")

        task = None
        if share.cifs_storage_task:
            task = share.cifs_storage_task

        vfs_objects = []
        if task:
            vfs_objects.append('shadow_copy2')
        extend_vfs_objects_for_zfs(share.cifs_path, vfs_objects)
        vfs_objects.extend(share.cifs_vfsobjects)

        if share.cifs_recyclebin:
            vfs_objects.append('recycle')
            confset1(smb4_shares, "recycle:repository = .recycle/%U")
            confset1(smb4_shares, "recycle:keeptree = yes")
            confset1(smb4_shares, "recycle:versions = yes")
            confset1(smb4_shares, "recycle:touch = yes")
            confset1(smb4_shares, "recycle:directory_mode = 0777")
            confset1(smb4_shares, "recycle:subdir_mode = 0700")

        if task:
            confset1(smb4_shares, "shadow:snapdir = .zfs/snapshot")
            confset1(smb4_shares, "shadow:sort = desc")
            confset1(smb4_shares, "shadow:localtime = yes")
            confset1(smb4_shares,
                     "shadow:format = auto-%%Y%%m%%d.%%H%%M-%s%s" %
                     (task.task_ret_count, task.task_ret_unit[0]))
            confset1(smb4_shares, "shadow:snapdirseverywhere = yes")

        config_share_for_vfs_objects(smb4_shares, vfs_objects)

        confset2(smb4_shares, "hide dot files = %s",
                 "no" if share.cifs_showhiddenfiles else "yes")
        confset2(smb4_shares, "hosts allow = %s", share.cifs_hostsallow)
        confset2(smb4_shares, "hosts deny = %s", share.cifs_hostsdeny)
        confset2(smb4_shares, "guest ok = %s",
                 "yes" if share.cifs_guestok else "no")

        confset2(smb4_shares, "guest only = %s",
                 "yes" if share.cifs_guestonly else False)

        config_share_for_nfs4(smb4_shares)
        config_share_for_zfs(smb4_shares)

        for line in share.cifs_auxsmbconf.split('\n'):
            line = line.strip()
            if not line:
                continue
            line = line.encode('utf-8')
            confset1(smb4_shares, line)


def generate_smb4_system_shares(client, smb4_shares):
    if client.call('notifier.common', 'system', 'domaincontroller_enabled'):
        try:
            dc = Struct(client.call('datastore.query', 'services.DomainController', None, {'get': True}))
            sysvol_path = "/var/db/samba4/sysvol"

            for share in ["sysvol", "netlogon"]:
                confset1(smb4_shares, "\n")
                confset1(smb4_shares, "[%s]" % (share), space=0)

                if share == "sysvol":
                    path = sysvol_path
                else:
                    path = "%s/%s/scripts" % (sysvol_path, dc.dc_realm.lower())

                confset1(smb4_shares, "path = %s" % (path))
                confset1(smb4_shares, "read only = no")

                vfs_objects = []

                extend_vfs_objects_for_zfs(path, vfs_objects)
                config_share_for_vfs_objects(smb4_shares, vfs_objects)

                config_share_for_nfs4(smb4_shares)
                config_share_for_zfs(smb4_shares)

        except:
            pass


def generate_smbusers(client):
    # FIXME: test query
    users = client.call('datastore.query', 'account.bsdusers', [
        ('bsdusr_microsoft_account', '=', True),
        ('bsdusr_email', '!=', None),
        ('bsdusr_email', '!=', ''),
    ])
    if not users:
        return

    with open("/usr/local/etc/smbusers", "w") as f:
        for u in users:
            u = Struct(u)
            f.write("%s = %s\n" % (u.bsdusr_username, u.bsdusr_email))
    os.chmod("/usr/local/etc/smbusers", 0644)


def provision_smb4():
    if not Samba4().domain_provision():
        print >> sys.stderr, "Failed to provision domain"
        return False

    if not Samba4().disable_password_complexity():
        print >> sys.stderr, "Failed to disable password complexity"
        return False

    if not Samba4().set_min_pwd_length():
        print >> sys.stderr, "Failed to set minimum password length"
        return False

    if not Samba4().set_administrator_password():
        print >> sys.stderr, "Failed to set administrator password"
        return False

    if not Samba4().domain_sentinel_file_create():
        return False

    return True


def smb4_mkdir(dir):
    try:
        os.makedirs(dir)
    except:
        pass


def smb4_unlink(dir):
    try:
        os.unlink(dir)
    except:
        pass


def smb4_setup(client):
    statedir = "/var/db/samba4"

    smb4_mkdir("/var/run/samba")
    smb4_mkdir("/var/db/samba")

    smb4_mkdir("/var/run/samba4")

    smb4_mkdir("/var/log/samba4")
    os.chmod("/var/log/samba4", 0755)

    smb4_unlink("/usr/local/etc/smb.conf")
    smb4_unlink("/usr/local/etc/smb4.conf")

    if not client.call('notifier.is_freenas') and client.call('notifier.failover_status') == 'BACKUP':
        return

    systemdataset_is_decrypted = client.call('notifier.systemdataset_is_decrypted')

    if not systemdataset_is_decrypted:
        if os.path.islink(statedir):
            smb4_unlink(statedir)
            smb4_mkdir(statedir)
        return

    systemdataset_path = client.call('notifier.system_dataset_path') or statedir

    basename_realpath = os.path.join(systemdataset_path, 'samba4')
    statedir_realpath = os.path.realpath(statedir)

    if os.path.islink(statedir) and not os.path.exists(statedir):
        smb4_unlink(statedir)

    if (basename_realpath != statedir_realpath and
            os.path.exists(basename_realpath)):
        smb4_unlink(statedir)
        if os.path.exists(statedir):
            olddir = "%s.%s" % (statedir, time.strftime("%Y%m%d%H%M%S"))
            try:
                os.rename(statedir, olddir)
            except Exception as e:
                print >> sys.stderr, "Unable to rename '%s' to '%s' (%s)" % (
                    statedir, olddir, e)
                sys.exit(1)

        try:
            os.symlink(basename_realpath, statedir)
        except Exception as e:
            print >> sys.stderr, ("Unable to create symlink '%s' -> '%s' (%s)"
                                  % (basename_realpath, statedir, e))
            sys.exit(1)

    if os.path.islink(statedir) and not os.path.exists(statedir_realpath):
        smb4_unlink(statedir)
        smb4_mkdir(statedir)

    smb4_mkdir("/var/db/samba4/private")
    os.chmod("/var/db/samba4/private", 0700)

    os.chmod(statedir, 0755)
#    smb4_set_SID()


def get_old_samba4_datasets(client):
    old_samba4_datasets = []

    fsvols = client.call('notifier.list_zfs_fsvols')
    for fsvol in fsvols:
        if re.match('^.+/.samba4\/?$', fsvol):
            old_samba4_datasets.append(fsvol)

    return old_samba4_datasets


def migration_available(old_samba4_datasets):
    res = False

    if old_samba4_datasets and len(old_samba4_datasets) == 1:
        res = True
    elif old_samba4_datasets:
        with open("/var/db/samba4/.alert_cant_migrate", "w") as f:
            f.close()

    return res


def do_migration(client, old_samba4_datasets):
    if len(old_samba4_datasets) > 1:
        return False
    old_samba4_dataset = "/mnt/%s/" % old_samba4_datasets[0]

    try:
        pipeopen("/usr/local/bin/rsync -avz '%s'* '/var/db/samba4/'" %
                 old_samba4_dataset).wait()
        client.call('notifier.destroy_zfs_dataset', old_samba4_datasets[0], True)

    except Exception as e:
        print >> sys.stderr, e

    return True


def smb4_import_users(client, smb_conf_path, smb4_tdb, exportfile=None):
    (fd, tmpfile) = tempfile.mkstemp(dir="/tmp")
    for line in smb4_tdb:
        os.write(fd, line + '\n')
    os.close(fd)

    args = [
        "/usr/local/bin/pdbedit",
        "-d 0",
        "-i smbpasswd:%s" % tmpfile,
        "-s %s" % smb_conf_path
    ]

    if exportfile is not None:
        # smb4_unlink(exportfile)
        args.append("-e tdbsam:%s" % exportfile)

    p = pipeopen(string.join(args, ' '))
    pdbedit_out = p.communicate()
    if pdbedit_out and pdbedit_out[0]:
        for line in pdbedit_out[0].split('\n'):
            line = line.strip()
            if not line:
                continue
            print line

    os.unlink(tmpfile)
    smb4_users = get_smb4_users(client)
    for u in smb4_users:
        u = Struct(u)
        smbhash = u.bsdusr_smbhash
        parts = smbhash.split(':')
        user = parts[0]

        flags = "-e"
        if u.bsdusr_locked or u.bsdusr_password_disabled:
            flags = "-d"

        p = pipeopen("/usr/local/bin/smbpasswd %s '%s'" % (flags, user))
        smbpasswd_out = p.communicate()

        if p.returncode != 0:
            print >> sys.stderr, "Failed to disable %s" % user
            continue

        if smbpasswd_out and smbpasswd_out[0]:
            for line in smbpasswd_out[0].split('\n'):
                line = line.strip()
                if not line:
                    continue
                print line


def smb4_grant_user_rights(user):
    args = [
        "/usr/local/bin/net",
        "-d 0",
        "sam",
        "rights",
        "grant"
    ]

    rights = [
        "SeTakeOwnershipPrivilege",
        "SeBackupPrivilege",
        "SeRestorePrivilege"
    ]

    net_cmd = "%s %s %s" % (
        string.join(args, ' '),
        user,
        string.join(rights, ' ')
    )

    p = pipeopen(net_cmd)
    net_out = p.communicate()
    if net_out and net_out[0]:
        for line in net_out[0].split('\n'):
            if not line:
                continue
            print line

    if p.returncode != 0:
        return False

    return True


def smb4_grant_rights():
    args = [
        "/usr/local/bin/pdbedit",
        "-d 0",
        "-L"
    ]

    p = pipeopen(string.join(args, ' '))
    pdbedit_out = p.communicate()
    if pdbedit_out and pdbedit_out[0]:
        for line in pdbedit_out[0].split('\n'):
            if not line:
                continue

            parts = line.split(':')
            user = parts[0]
            smb4_grant_user_rights(user)


def get_groups(client):
    _groups = {}

    groups = client.call('datastore.query', 'account.bsdGroups', [('bsdgrp_builtin', '=', False)])
    for g in groups:
        g = Struct(g)
        key = str(g.bsdgrp_group)
        _groups[key] = []
        # FIXME: test query
        members = client.call('datastore.query', 'account.bsdGroupMembership', [('bsdgrpmember_group', '=', g.id)])
        for m in members:
            m = Struct(m)
            # FIXME: test query
            u = client.call('datastore.query', 'account.bsdUsers', [('bsdusr_username', '=', m.bsdgrpmember_user)])
            if u:
                u = u[0]
                _groups[key].append(str(u['bsdusr_username']))

    return _groups


def smb4_import_groups(client):
    # XXX: WTF moment, this method is not used
    s = Samba4()

    groups = get_groups(client)
    for g in groups:
        s.group_add(g)
        if groups[g]:
            s.group_addmembers(g, groups[g])


def smb4_group_mapped(groupmap, group):
    if not groupmap:
        return False

    for gm in groupmap:
        unixgroup = gm['unixgroup']
        if group == unixgroup:
            return True

    return False


# Windows no likey
def smb4_groupname_is_username(group):
    try:
        pwd.getpwnam(group)
    except KeyError:
        return False

    return True


def smb4_map_groups(client):
    groupmap = client.call('notifier.groupmap_list')
    groups = get_groups(client)
    for g in groups:
        if not (smb4_group_mapped(groupmap, g) or smb4_groupname_is_username(g)):
            client.call('notifier.groupmap_add', g, g)


def smb4_backup_tdbfile(tdb_src, tdb_dst):
    try:
        db_r = tdb.open(tdb_src, flags=os.O_RDONLY)

    except Exception as e:
        print >> sys.stderr, "Unable to open %s: %s" % (tdb_src, e)
        log.error("Unable to open {0}: {1}".format(tdb_src, e))
        log_traceback(log=log)
        return False

    try:
        db_w = tdb.open(tdb_dst, flags=os.O_RDWR | os.O_CREAT, mode=0600)

    except Exception as e:
        print >> sys.stderr, "Unable to open %s: %s" % (tdb_dst, e)
        log.error("Unable to open {0}: {1}".format(tdb_dst, e))
        log_traceback(log=log)
        return False

    for key in db_r.iterkeys():
        try:
            db_w.transaction_start()
            db_w[key] = db_r.get(key)
            db_w.transaction_prepare_commit()
            db_w.transaction_commit()

        except Exception as e:
            print >> sys.stderr, "Transaction for key %s failed: %s" % (key, e)
            log.error("Transaction for key {0} failed: {1}".format(key, e))
            log_traceback(log=log)
            db_w.transaction_cancel()

    db_r.close()
    db_w.close()

    return True


def smb4_restore_tdbfile(tdb_src, tdb_dst):
    try:
        db_r = tdb.open(tdb_src, flags=os.O_RDONLY)

    except Exception as e:
        print >> sys.stderr, "Unable to open %s: %s" % (tdb_src, e)
        log.error("Unable to open {0}: {1}".format(tdb_src, e))
        log_traceback(log=log)
        return False

    try:
        db_w = tdb.open(tdb_dst, flags=os.O_RDWR)
    except Exception as e:
        print >> sys.stderr, "Unable to open %s: %s" % (tdb_dst, e)
        log.error("Unable to open {0}: {1}".format(tdb_dst, e))
        log_traceback(log=log)
        return False

    for key in db_r.iterkeys():
        try:
            db_w.transaction_start()

            db_w.lock_all()
            db_w[key] = db_r.get(key)
            db_w.unlock_all()

            db_w.transaction_prepare_commit()
            db_w.transaction_commit()

        except Exception as e:
            print >> sys.stderr, "Transaction for key %s failed: %s" % (key, e)
            log.error("Transaction for key {0} failed: {1}".format(key, e))
            log_traceback(log=log)
            db_w.transaction_cancel()

    db_r.close()
    db_w.close()

    return True


def backup_secrets_database():
    secrets = '/var/db/samba4/private/secrets.tdb'
    backup = '/root/secrets.tdb'

    smb4_backup_tdbfile(secrets, backup)


def restore_secrets_database():
    secrets = '/var/db/samba4/private/secrets.tdb'
    backup = '/root/secrets.tdb'

    smb4_restore_tdbfile(backup, secrets)


def main():
    client = Client()
    smb_conf_path = "/usr/local/etc/smb4.conf"

    smb4_tdb = []
    smb4_conf = []
    smb4_shares = []

    backup_secrets_database()
    smb4_setup(client)

    old_samba4_datasets = get_old_samba4_datasets(client)
    if migration_available(old_samba4_datasets):
        do_migration(client, old_samba4_datasets)

    role = get_server_role(client)

    generate_smbusers()
    generate_smb4_tdb(client, smb4_tdb)
    generate_smb4_conf(client, smb4_conf, role)
    generate_smb4_system_shares(client, smb4_shares)
    generate_smb4_shares(client, smb4_shares)

    if role == 'dc' and not Samba4().domain_provisioned():
        provision_smb4()

    with open(smb_conf_path, "w") as f:
        for line in smb4_conf:
            f.write(line + '\n')
        for line in smb4_shares:
            f.write(line + '\n')

    smb4_set_SID(client)

    if role == 'member' and smb4_ldap_enabled():
        set_ldap_password(client)
        backup_secrets_database()

    if role != 'dc':
        if not Samba4().users_imported():
            smb4_import_users(
                client,
                smb_conf_path,
                smb4_tdb,
                "/var/db/samba4/private/passdb.tdb"
            )
            smb4_grant_rights()
            Samba4().user_import_sentinel_file_create()

        smb4_map_groups(client)

    if role == 'member' and client.call('notifier.common', 'system', 'activedirectory_enabled') and idmap_backend_rfc2307(client):
        set_idmap_rfc2307_secret(client)

    restore_secrets_database()

if __name__ == '__main__':
    main()
