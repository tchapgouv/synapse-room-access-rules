# -*- coding: utf-8 -*-
# Copyright 2021 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import Any, Dict, Optional

import aiounittest
from synapse.module_api.errors import SynapseError
from synapse.types import JsonDict

from room_access_rules import (
    ACCESS_RULES_TYPE,
    AccessRules,
    EventTypes,
    create_state_map,
)
from tests import MockRequester, create_module


class RoomCreateTestCase(aiounittest.AsyncTestCase):
    def setUp(self) -> None:
        self.module = create_module()
        self.user_id = "@mark:example.com"

    async def test_create_room_no_rule(self):
        """Tests that creating a room without specifying a rule defaults to the room's
        rule being "restricted".
        """
        room_config = await self._create_room(direct=False, rule=AccessRules.RESTRICTED)
        self._check_rule_and_encryption(room_config, AccessRules.RESTRICTED)

    async def test_create_room_unencrypted_private(self):
        """Tests that creating a private room with encrypted at false in our custom event works well."""
        room_config = await self._create_room(
            direct=False,
            initial_state=[
                {
                    "type": ACCESS_RULES_TYPE,
                    "state_key": "",
                    "content": {
                        "rule": AccessRules.RESTRICTED,
                        "encrypted": False,
                    },
                }
            ],
        )
        self._check_rule_and_encryption(room_config, AccessRules.RESTRICTED, False)

    async def test_create_room_direct_no_rule(self):
        """Tests that creating a DM without specifying a rule defaults to the room's
        rule being "direct".
        """
        room_config = await self._create_room(direct=True, rule=AccessRules.DIRECT)
        self._check_rule_and_encryption(room_config, AccessRules.DIRECT)

    async def test_create_room_valid_rule(self):
        """Tests that creating a room with a valid rule for the creation configuration
        works.
        """
        await self._create_room(direct=False, rule=AccessRules.RESTRICTED)

    async def test_create_room_invalid_rule(self):
        """Tests that creating a room with an invalid rule for the creation configuration
        raises an exception.
        """
        with self.assertRaises(SynapseError):
            await self._create_room(direct=False, rule=AccessRules.DIRECT)

    async def test_create_room_direct_invalid_rule(self):
        """Tests that creating a DM with an invalid rule for the creation configuration
        raises an exception.
        """
        with self.assertRaises(SynapseError):
            await self._create_room(direct=True, rule=AccessRules.RESTRICTED)

    async def test_create_room_default_power_level_rules(self):
        """Tests that creating a room without overriding the power levels means the module
        adds default power levels to the room creation config that differ from the default
        values in the Matrix specification.
        """
        config = await self._create_room()

        self.assertIn("power_level_content_override", config)

        pl_override = config["power_level_content_override"]
        self.assertEqual(pl_override["state_default"], 100, pl_override)
        self.assertEqual(pl_override["invite"], 50, pl_override)

    async def test_create_room_fails_on_incorrect_power_level_rules(self):
        """Tests that creating a room with a power levels override that would set
        'state_default' and/or 'invite' to values too low to be allowed raises an
        exception.
        """
        pl_override_state_default = self.module._get_default_power_levels(self.user_id)
        pl_override_state_default["state_default"] = 50

        pl_override_invite = self.module._get_default_power_levels(self.user_id)
        pl_override_invite["invite"] = 0

        # Test that overriding the state_default value via power_level_content_override
        # fails.
        with self.assertRaises(SynapseError):
            await self._create_room(power_levels_override=pl_override_state_default)

        # Test that overriding the invite value via power_level_content_override fails.
        with self.assertRaises(SynapseError):
            await self._create_room(power_levels_override=pl_override_invite)

        # Test that overriding the state_default value via initial_state fails.
        with self.assertRaises(SynapseError):
            await self._create_room(
                initial_state=[
                    {
                        "type": "m.room.power_levels",
                        "state_key": "",
                        "content": pl_override_state_default,
                    }
                ],
            )

        # Test that overriding the invite value via initial_state fails.
        with self.assertRaises(SynapseError):
            await self._create_room(
                initial_state=[
                    {
                        "type": EventTypes.PowerLevels,
                        "state_key": "",
                        "content": pl_override_invite,
                    }
                ],
            )

    async def test_create_room_with_missing_power_levels_use_default_values(self):
        """Tests that a room created with custom power levels, but without defining
        invite or state_default succeeds, but the missing values are replaced with the
        defaults.
        """
        # Test that the defaults are correctly set when the custom PL is set using
        # power_level_content_override.
        pl_override = self.module._get_default_power_levels(self.user_id)
        del pl_override["state_default"]
        del pl_override["invite"]

        config = await self._create_room(power_levels_override=pl_override)
        self.assertIn("power_level_content_override", config)
        self.assertEqual(
            config["power_level_content_override"]["state_default"],
            100,
            pl_override,
        )
        self.assertEqual(
            config["power_level_content_override"]["invite"],
            50,
            pl_override,
        )

        # Test that the defaults are correctly set when the custom PL is set using
        # initial_state.
        pl_override = self.module._get_default_power_levels(self.user_id)
        del pl_override["state_default"]
        del pl_override["invite"]

        config = await self._create_room(
            initial_state=[
                {
                    "type": EventTypes.PowerLevels,
                    "state_key": "",
                    "content": pl_override,
                }
            ],
        )
        initial_state_map = create_state_map(config["initial_state"])

        pl_event = initial_state_map.get((EventTypes.PowerLevels, ""))
        self.assertIsNotNone(pl_event)
        self.assertEqual(
            pl_event["content"]["state_default"],
            100,
            pl_override,
        )
        self.assertEqual(
            pl_event["content"]["invite"],
            50,
            pl_override,
        )

    async def _create_room(
        self,
        direct: bool = False,
        rule: str | None = None,
        power_levels_override: Optional[dict] = None,
        initial_state: Optional[list] = None,
    ) -> Dict[str, Any]:
        config = {
            "is_direct": direct,
            # TODO handle public
            "preset": "trusted_private_chat" if direct else "private_chat",
            "initial_state": [],
        }

        if rule:
            config["initial_state"] = [
                {
                    "type": ACCESS_RULES_TYPE,
                    "state_key": "",
                    "content": {
                        "rule": rule,
                    },
                }
            ]

        if initial_state is not None:
            config["initial_state"] = config["initial_state"] + initial_state

        if power_levels_override is not None:
            config["power_level_content_override"] = power_levels_override

        await self.module.on_create_room(
            requester=MockRequester(self.user_id),
            config=config,
            is_requester_admin=False,
        )

        return config

    def _check_rule_and_encryption(
        self, room_config: JsonDict, expected_rule: str, expected_encrypted: bool = True
    ):
        initial_state = create_state_map(room_config.get("initial_state"))

        access_rule_event = initial_state.get((ACCESS_RULES_TYPE, ""))
        self.assertIsNotNone(access_rule_event)
        self.assertEqual(access_rule_event["content"]["rule"], expected_rule)

        if expected_encrypted:
            encryption_event = initial_state.get((EventTypes.RoomEncryption, ""))
            self.assertIsNotNone(encryption_event)
            self.assertEqual(
                encryption_event["content"]["algorithm"], "m.megolm.v1.aes-sha2"
            )
        else:
            self.assertNotIn((EventTypes.RoomEncryption, ""), initial_state)
