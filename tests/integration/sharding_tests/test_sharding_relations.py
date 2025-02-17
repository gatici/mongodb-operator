#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
import pytest
from juju.errors import JujuAPIError
from pytest_operator.plugin import OpsTest

SHARD_ONE_APP_NAME = "shard"
CONFIG_SERVER_ONE_APP_NAME = "config-server-one"
CONFIG_SERVER_TWO_APP_NAME = "config-server-two"
REPLICATION_APP_NAME = "replication"
APP_CHARM_NAME = "application"
LEGACY_APP_CHARM_NAME = "legacy-application"

SHARDING_COMPONENTS = [SHARD_ONE_APP_NAME, CONFIG_SERVER_ONE_APP_NAME]

CONFIG_SERVER_REL_NAME = "config-server"
SHARD_REL_NAME = "sharding"
DATABASE_REL_NAME = "first-database"
LEGACY_RELATION_NAME = "obsolete"

RELATION_LIMIT_MESSAGE = 'cannot add relation "shard:sharding config-server-two:config-server": establishing a new relation for shard:sharding would exceed its maximum relation limit of 1'
# for now we have a large timeout due to the slow drainage of the `config.system.sessions`
# collection. More info here:
# https://stackoverflow.com/questions/77364840/mongodb-slow-chunk-migration-for-collection-config-system-sessions-with-remov
TIMEOUT = 30 * 60


@pytest.mark.abort_on_fail
async def test_build_and_deploy(
    ops_test: OpsTest, application_charm, legacy_charm, database_charm
) -> None:
    """Build and deploy a sharded cluster."""
    await ops_test.model.deploy(
        database_charm,
        config={"role": "config-server"},
        application_name=CONFIG_SERVER_ONE_APP_NAME,
    )
    await ops_test.model.deploy(
        database_charm,
        config={"role": "config-server"},
        application_name=CONFIG_SERVER_TWO_APP_NAME,
    )
    await ops_test.model.deploy(
        database_charm, config={"role": "shard"}, application_name=SHARD_ONE_APP_NAME
    )
    await ops_test.model.deploy(database_charm, application_name=REPLICATION_APP_NAME)
    await ops_test.model.deploy(application_charm, application_name=APP_CHARM_NAME)
    await ops_test.model.deploy(legacy_charm, application_name=LEGACY_APP_CHARM_NAME)

    await ops_test.model.wait_for_idle(
        apps=[
            CONFIG_SERVER_ONE_APP_NAME,
            CONFIG_SERVER_TWO_APP_NAME,
            SHARD_ONE_APP_NAME,
        ],
        idle_period=20,
        raise_on_blocked=False,
        timeout=TIMEOUT,
    )


async def test_only_one_config_server_relation(ops_test: OpsTest) -> None:
    """Verify that a shard can only be related to one config server."""
    await ops_test.model.integrate(
        f"{SHARD_ONE_APP_NAME}:{SHARD_REL_NAME}",
        f"{CONFIG_SERVER_ONE_APP_NAME}:{CONFIG_SERVER_REL_NAME}",
    )

    with pytest.raises(JujuAPIError) as juju_error:
        await ops_test.model.integrate(
            f"{SHARD_ONE_APP_NAME}:{SHARD_REL_NAME}",
            f"{CONFIG_SERVER_TWO_APP_NAME}:{CONFIG_SERVER_REL_NAME}",
        )

    assert (
        juju_error.value.args[0] == RELATION_LIMIT_MESSAGE
    ), "Shard can relate to multiple config servers."

    # clean up relation
    await ops_test.model.applications[SHARD_ONE_APP_NAME].remove_relation(
        f"{SHARD_ONE_APP_NAME}:{SHARD_REL_NAME}",
        f"{CONFIG_SERVER_ONE_APP_NAME}:{CONFIG_SERVER_REL_NAME}",
    )

    await ops_test.model.wait_for_idle(
        apps=[REPLICATION_APP_NAME],
        idle_period=20,
        raise_on_blocked=False,
        timeout=TIMEOUT,
    )


async def test_cannot_use_db_relation(ops_test: OpsTest) -> None:
    """Verify that sharding components cannot use the DB relation."""
    for sharded_component in SHARDING_COMPONENTS:
        await ops_test.model.integrate(f"{APP_CHARM_NAME}:{DATABASE_REL_NAME}", sharded_component)

    await ops_test.model.wait_for_idle(
        apps=SHARDING_COMPONENTS,
        idle_period=20,
        raise_on_blocked=False,
        timeout=TIMEOUT,
    )

    for sharded_component in SHARDING_COMPONENTS:
        sharded_component_unit = ops_test.model.applications[sharded_component].units[0]
        assert (
            sharded_component_unit.workload_status_message
            == "Sharding roles do not support database interface."
        ), f"{sharded_component} cannot be related using the database relation"

    # clean up relations
    for sharded_component in SHARDING_COMPONENTS:
        await ops_test.model.applications[sharded_component].remove_relation(
            f"{APP_CHARM_NAME}:{DATABASE_REL_NAME}",
            sharded_component,
        )

    await ops_test.model.wait_for_idle(
        apps=SHARDING_COMPONENTS,
        idle_period=20,
        raise_on_blocked=False,
        timeout=TIMEOUT,
    )


async def test_cannot_use_legacy_db_relation(ops_test: OpsTest) -> None:
    """Verify that sharding components cannot use the legacy DB relation."""
    for sharded_component in SHARDING_COMPONENTS:
        await ops_test.model.integrate(LEGACY_APP_CHARM_NAME, sharded_component)

    await ops_test.model.wait_for_idle(
        apps=SHARDING_COMPONENTS,
        idle_period=20,
        raise_on_blocked=False,
        timeout=TIMEOUT,
    )

    for sharded_component in SHARDING_COMPONENTS:
        sharded_component_unit = ops_test.model.applications[sharded_component].units[0]
        assert (
            sharded_component_unit.workload_status_message
            == "Sharding roles do not support obsolete interface."
        ), f"{sharded_component} cannot be related using the mongodb relation"

    # clean up relations
    for sharded_component in SHARDING_COMPONENTS:
        await ops_test.model.applications[sharded_component].remove_relation(
            f"{sharded_component}:{LEGACY_RELATION_NAME}",
            f"{LEGACY_APP_CHARM_NAME}:{LEGACY_RELATION_NAME}",
        )

    await ops_test.model.wait_for_idle(
        apps=SHARDING_COMPONENTS,
        idle_period=20,
        raise_on_blocked=False,
        timeout=TIMEOUT,
    )


async def test_replication_config_server_relation(ops_test: OpsTest):
    """Verifies that using a replica as a shard fails."""
    # attempt to add a replication deployment as a shard to the config server.
    await ops_test.model.integrate(
        f"{REPLICATION_APP_NAME}:{SHARD_REL_NAME}",
        f"{CONFIG_SERVER_ONE_APP_NAME}:{CONFIG_SERVER_REL_NAME}",
    )

    await ops_test.model.wait_for_idle(
        apps=[REPLICATION_APP_NAME],
        idle_period=20,
        raise_on_blocked=False,
        timeout=TIMEOUT,
    )

    replication_unit = ops_test.model.applications[REPLICATION_APP_NAME].units[0]
    assert (
        replication_unit.workload_status_message == "sharding interface cannot be used by replicas"
    ), "replication cannot be related to config server."

    # clean up relations
    await ops_test.model.applications[REPLICATION_APP_NAME].remove_relation(
        f"{REPLICATION_APP_NAME}:{SHARD_REL_NAME}",
        f"{CONFIG_SERVER_ONE_APP_NAME}:{CONFIG_SERVER_REL_NAME}",
    )


async def test_replication_shard_relation(ops_test: OpsTest):
    """Verifies that using a replica as a config-server fails."""
    # attempt to add a shard to a replication deployment as a config server.
    await ops_test.model.integrate(
        f"{SHARD_ONE_APP_NAME}:{SHARD_REL_NAME}",
        f"{REPLICATION_APP_NAME}:{CONFIG_SERVER_REL_NAME}",
    )

    await ops_test.model.wait_for_idle(
        apps=[REPLICATION_APP_NAME],
        idle_period=20,
        raise_on_blocked=False,
        timeout=TIMEOUT,
    )

    replication_unit = ops_test.model.applications[REPLICATION_APP_NAME].units[0]
    assert (
        replication_unit.workload_status_message == "sharding interface cannot be used by replicas"
    ), "replication cannot be related to config server."

    # clean up relation
    await ops_test.model.applications[REPLICATION_APP_NAME].remove_relation(
        f"{SHARD_ONE_APP_NAME}:{SHARD_REL_NAME}",
        f"{REPLICATION_APP_NAME}:{CONFIG_SERVER_REL_NAME}",
    )

    await ops_test.model.wait_for_idle(
        apps=[REPLICATION_APP_NAME],
        idle_period=20,
        raise_on_blocked=False,
        timeout=TIMEOUT,
    )
