"""Simple functions, which can be used in both K8s and VM charms."""

# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
import json
import logging
import os
import secrets
import string
import subprocess
from typing import List

from charms.mongodb.v0.mongodb import MongoDBConfiguration, MongoDBConnection
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    StatusBase,
    WaitingStatus,
)
from pymongo.errors import AutoReconnect, ServerSelectionTimeoutError

from config import Config

# The unique Charmhub library identifier, never change it
LIBID = "b9a7fe0c38d8486a9d1ce94c27d4758e"

# Increment this major API version when introducing breaking changes
LIBAPI = 1

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 3

# path to store mongodb ketFile
KEY_FILE = "keyFile"
TLS_EXT_PEM_FILE = "external-cert.pem"
TLS_EXT_CA_FILE = "external-ca.crt"
TLS_INT_PEM_FILE = "internal-cert.pem"
TLS_INT_CA_FILE = "internal-ca.crt"

MONGODB_COMMON_DIR = "/var/snap/charmed-mongodb/common"
MONGODB_SNAP_DATA_DIR = "/var/snap/charmed-mongodb/current"

MONGO_SHELL = "charmed-mongodb.mongosh"

DATA_DIR = "/var/lib/mongodb"
LOG_DIR = "/var/log/mongodb"
LOG_TO_SYSLOG = True
CONF_DIR = "/etc/mongod"
MONGODB_LOG_FILENAME = "mongodb.log"
logger = logging.getLogger(__name__)


def _get_logging_options(snap_install: bool) -> str:
    # TODO sending logs to syslog until we have a separate mount point for logs
    if LOG_TO_SYSLOG:
        return ""
    # in k8s the default logging options that are used for the vm charm are ignored and logs are
    # the output of the container. To enable logging to a file it must be set explicitly
    return f"--logpath={LOG_DIR}/{MONGODB_LOG_FILENAME}" if snap_install else ""


# noinspection GrazieInspection
def get_create_user_cmd(config: MongoDBConfiguration, mongo_path=MONGO_SHELL) -> List[str]:
    """Creates initial admin user for MongoDB.

    Initial admin user can be created only through localhost connection.
    see https://www.mongodb.com/docs/manual/core/localhost-exception/
    unfortunately, pymongo not able to create connection which considered
    as local connection by MongoDB, even if socket connection used.
    As result where are only hackish ways to create initial user.
    It is needed to install mongodb-clients inside charm container to make
    this function work correctly
    """
    return [
        mongo_path,
        "mongodb://localhost/admin",
        "--quiet",
        "--eval",
        "db.createUser({"
        f"  user: '{config.username}',"
        "  pwd: passwordPrompt(),"
        "  roles:["
        "    {'role': 'userAdminAnyDatabase', 'db': 'admin'}, "
        "    {'role': 'readWriteAnyDatabase', 'db': 'admin'}, "
        "    {'role': 'clusterAdmin', 'db': 'admin'}, "
        "  ],"
        "  mechanisms: ['SCRAM-SHA-256'],"
        "  passwordDigestor: 'server',"
        "})",
    ]


def get_mongos_args(
    config,
    snap_install: bool = False,
    config_server_db: str = None,
) -> str:
    """Returns the arguments used for starting mongos on a config-server side application.

    Returns:
        A string representing the arguments to be passed to mongos.
    """
    # suborinate charm which provides its own config_server_db, should only use unix domain socket
    binding_ips = (
        f"--bind_ip {MONGODB_COMMON_DIR}/var/mongodb-27018.sock"
        if config_server_db
        else "--bind_ip_all"
    )

    # mongos running on the config server communicates through localhost
    config_server_db = config_server_db or f"{config.replset}/localhost:{Config.MONGODB_PORT}"

    full_conf_dir = f"{MONGODB_SNAP_DATA_DIR}{CONF_DIR}" if snap_install else CONF_DIR
    cmd = [
        # mongos on config server side should run on 0.0.0.0 so it can be accessed by other units
        # in the sharded cluster
        binding_ips,
        f"--configdb {config_server_db}",
        # config server is already using 27017
        f"--port {Config.MONGOS_PORT}",
        f"--keyFile={full_conf_dir}/{KEY_FILE}",
        "\n",
    ]

    # TODO Future PR: support TLS on mongos

    return " ".join(cmd)


def get_mongod_args(
    config: MongoDBConfiguration,
    auth: bool = True,
    snap_install: bool = False,
    role: str = "replication",
) -> str:
    """Construct the MongoDB startup command line.

    Returns:
        A string representing the command used to start MongoDB.
    """
    full_data_dir = f"{MONGODB_COMMON_DIR}{DATA_DIR}" if snap_install else DATA_DIR
    full_conf_dir = f"{MONGODB_SNAP_DATA_DIR}{CONF_DIR}" if snap_install else CONF_DIR
    logging_options = _get_logging_options(snap_install)
    cmd = [
        # bind to localhost and external interfaces
        "--bind_ip_all",
        # part of replicaset
        f"--replSet={config.replset}",
        # db must be located within the snap common directory since the snap is strictly confined
        f"--dbpath={full_data_dir}",
        # for simplicity we run the mongod daemon on shards, configsvrs, and replicas on the same
        # port
        f"--port={Config.MONGODB_PORT}",
        "--auditDestination=syslog",  # TODO sending logs to syslog until we have a separate mount point for logs
        f"--auditFormat={Config.AuditLog.FORMAT}",
        logging_options,
    ]
    if auth:
        cmd.extend(["--auth"])

    if auth and not config.tls_internal:
        # keyFile cannot be used without auth and cannot be used in tandem with internal TLS
        cmd.extend(
            [
                "--clusterAuthMode=keyFile",
                f"--keyFile={full_conf_dir}/{KEY_FILE}",
            ]
        )

    if config.tls_external:
        cmd.extend(
            [
                f"--tlsCAFile={full_conf_dir}/{TLS_EXT_CA_FILE}",
                f"--tlsCertificateKeyFile={full_conf_dir}/{TLS_EXT_PEM_FILE}",
                # allow non-TLS connections
                "--tlsMode=preferTLS",
                "--tlsDisabledProtocols=TLS1_0,TLS1_1",
            ]
        )

    # internal TLS can be enabled only in external is enabled
    if config.tls_internal and config.tls_external:
        cmd.extend(
            [
                "--clusterAuthMode=x509",
                "--tlsAllowInvalidCertificates",
                f"--tlsClusterCAFile={full_conf_dir}/{TLS_INT_CA_FILE}",
                f"--tlsClusterFile={full_conf_dir}/{TLS_INT_PEM_FILE}",
            ]
        )

    if role == Config.Role.CONFIG_SERVER:
        cmd.append("--configsvr")

    if role == Config.Role.SHARD:
        cmd.append("--shardsvr")

    cmd.append("\n")
    return " ".join(cmd)


def generate_password() -> str:
    """Generate a random password string.

    Returns:
       A random password string.
    """
    choices = string.ascii_letters + string.digits
    return "".join([secrets.choice(choices) for _ in range(32)])


def generate_keyfile() -> str:
    """Key file used for authentication between replica set peers.

    Returns:
       A maximum allowed random string.
    """
    choices = string.ascii_letters + string.digits
    return "".join([secrets.choice(choices) for _ in range(1024)])


def build_unit_status(mongodb_config: MongoDBConfiguration, unit_ip: str) -> StatusBase:
    """Generates the status of a unit based on its status reported by mongod."""
    try:
        with MongoDBConnection(mongodb_config) as mongo:
            replset_status = mongo.get_replset_status()

            if unit_ip not in replset_status:
                return WaitingStatus("Member being added..")

            replica_status = replset_status[unit_ip]

            if replica_status == "PRIMARY":
                return ActiveStatus("Primary")
            elif replica_status == "SECONDARY":
                return ActiveStatus("")
            elif replica_status in ["STARTUP", "STARTUP2", "ROLLBACK", "RECOVERING"]:
                return WaitingStatus("Member is syncing...")
            elif replica_status == "REMOVED":
                return WaitingStatus("Member is removing...")
            else:
                return BlockedStatus(replica_status)
    except ServerSelectionTimeoutError as e:
        # ServerSelectionTimeoutError is commonly due to ReplicaSetNoPrimary
        logger.debug("Got error: %s, while checking replica set status", str(e))
        return WaitingStatus("Waiting for primary re-election..")
    except AutoReconnect as e:
        # AutoReconnect is raised when a connection to the database is lost and an attempt to
        # auto-reconnect will be made by pymongo.
        logger.debug("Got error: %s, while checking replica set status", str(e))
        return WaitingStatus("Waiting to reconnect to unit..")


def copy_licenses_to_unit():
    """Copies licenses packaged in the snap to the charm's licenses directory."""
    os.makedirs("src/licenses", exist_ok=True)
    subprocess.check_output("cp LICENSE src/licenses/LICENSE-charm", shell=True)
    subprocess.check_output(
        "cp -r /snap/charmed-mongodb/current/licenses/* src/licenses", shell=True
    )


def current_pbm_op(pbm_status: str) -> str:
    """Parses pbm status for the operation that pbm is running."""
    pbm_status = json.loads(pbm_status)
    return pbm_status["running"] if "running" in pbm_status else ""


def process_pbm_status(pbm_status: str) -> StatusBase:
    """Parses current pbm operation and returns unit status."""
    current_op = current_pbm_op(pbm_status)
    # no operations are currently running with pbm
    if current_op == {}:
        return ActiveStatus("")

    if current_op["type"] == "backup":
        backup_id = current_op["name"]
        return MaintenanceStatus(f"backup started/running, backup id:'{backup_id}'")

    if current_op["type"] == "restore":
        backup_id = current_op["name"]
        return MaintenanceStatus(f"restore started/running, backup id:'{backup_id}'")

    if current_op["type"] == "resync":
        return WaitingStatus("waiting to sync s3 configurations.")

    return ActiveStatus()


def add_args_to_env(var: str, args: str):
    """Adds the provided arguments to the environment as the provided variable."""
    with open(Config.ENV_VAR_PATH, "r") as env_var_file:
        env_vars = env_var_file.readlines()

    args_added = False
    for index, line in enumerate(env_vars):
        if var in line:
            args_added = True
            env_vars[index] = f"{var}={args}"

    # if it is the first time adding these args to the file - will will need to append them to the
    # file
    if not args_added:
        env_vars.append(f"{var}={args}")

    with open(Config.ENV_VAR_PATH, "w") as service_file:
        service_file.writelines(env_vars)
