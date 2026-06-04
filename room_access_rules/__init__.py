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
import email.utils
import logging
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Tuple,
)

import attr
from synapse.api.constants import (
    EventTypes,
    JoinRules,
    Membership,
    RoomCreationPreset,
    RoomEncryptionAlgorithms,
)
from synapse.api.errors import Codes
from synapse.events import EventBase
from synapse.module_api import ModuleApi, UserID
from synapse.module_api.errors import ConfigError, SynapseError
from synapse.storage.database import LoggingTransaction
from synapse.types import (
    JsonDict,
    JsonMapping,
    MutableStateMap,
    Requester,
    ScheduledTask,
    StateMap,
    TaskStatus,
    get_domain_from_id,
)
from synapse.util.frozenutils import unfreeze

logger = logging.getLogger(__name__)

ACCESS_RULES_TYPE = "im.vector.room.access_rules"

LOCATION_LIVE_SHARE_TYPE = "m.beacon_info"
LOCATION_LIVE_SHARE_MSC_TYPE = "org.matrix.msc3672.beacon_info"

MATRIX_RTC_CALL_MEMBER_TYPE = "m.call.member"
MATRIX_RTC_CALL_MEMBER_MSC_TYPE = "org.matrix.msc3401.call.member"


class AccessRules:
    DIRECT = "direct"
    RESTRICTED = "restricted"
    UNRESTRICTED = "unrestricted"


VALID_ACCESS_RULES = (
    AccessRules.DIRECT,
    AccessRules.RESTRICTED,
    AccessRules.UNRESTRICTED,
)

# Rules to which we need to apply the power levels restrictions.
#
# These are all of the rules that neither:
#  * forbid users from joining based on a server blacklist (which means that there
#     is no need to apply power level restrictions), nor
#  * target direct chats (since we allow both users to be room admins in this case).
#
# The power-level restrictions, when they are applied, prevent the following:
#  * the default power level for users (users_default) being set to anything other than 0.
#  * a non-default power level being assigned to any user which would be forbidden from
#     joining a restricted room.
RULES_WITH_RESTRICTED_POWER_LEVELS = (AccessRules.UNRESTRICTED,)


class Visibility:
    PUBLIC = "public"
    PRIVATE = "private"


@attr.s(frozen=True, auto_attribs=True)
class RoomAccessRulesConfig:
    id_server: str
    bypass_for_users: List[str] = []
    domains_forbidden_when_restricted: List[str] = []
    fix_admins_for_dm_power_levels: bool = False
    add_live_location_power_levels: bool = False
    add_matrix_rtc_call_power_levels: bool = False
    fix_visibility_access_rules: bool = False


class RoomAccessRules(object):
    """Implementation of the ThirdPartyEventRules module API that allows federation admins
    to define custom rules for specific events and actions.
    Implements the custom behaviour for the "im.vector.room.access_rules" state event.

    Don't forget to consider if you can invite users from your own domain.
    """

    def __init__(
        self,
        config: RoomAccessRulesConfig,
        api: ModuleApi,
    ):
        self.config = config
        self.module_api = api

        # We keep using check_visibility_can_be_modified because user_may_publish_room
        # doesn't receive the new visibility state that we need to check.
        # We keep using on_create_room because user_may_create_room doesn't allow us to
        # change the initial state (needed to force the encryption or the visibility).
        self.module_api.register_third_party_rules_callbacks(
            on_create_room=self.on_create_room,
            check_visibility_can_be_modified=self.check_visibility_can_be_modified,
        )
        self.module_api.register_spam_checker_callbacks(
            check_event_for_spam=self.check_event_for_spam,
            user_may_send_3pid_invite=self.user_may_send_3pid_invite,
        )

        self.task_scheduler = api._hs.get_task_scheduler()
        self.store = api._hs.get_datastores().main
        self.storage_controllers = api._hs.get_storage_controllers()

        self.task_scheduler.register_action(
            self.fix_existing_rooms_power_levels,
            "fix_existing_rooms_power_levels",
        )

        self.task_scheduler.register_action(
            self.fix_existing_rooms_visibility_access_rules,
            "fix_existing_rooms_visibility_access_rules",
        )

        # Only schedules if we are the main process so we only queue one job per restart,
        # but the job itself will be run on the background worker if available
        if (
            config.fix_admins_for_dm_power_levels
            or config.add_live_location_power_levels
            or config.add_matrix_rtc_call_power_levels
        ) and api.worker_name is None:

            async def schedule_task() -> None:
                await self.task_scheduler.schedule_task(
                    "fix_existing_rooms_power_levels"
                )

            api.delayed_background_call(0, schedule_task)

        if config.fix_visibility_access_rules and api.worker_name is None:

            async def schedule_task() -> None:
                await self.task_scheduler.schedule_task(
                    "fix_existing_rooms_visibility_access_rules"
                )

            api.delayed_background_call(0, schedule_task)

    @staticmethod
    def parse_config(config_dict: Dict[str, Any]) -> RoomAccessRulesConfig:
        """Parses and validates the options specified in the homeserver config.

        Args:
            config_dict: The config dict.

        Returns:
            The parsed config.

        Raises:
            ConfigError: If there was an issue with the provided module configuration.
        """
        if "id_server" not in config_dict:
            raise ConfigError("No IS for event rules RoomAccessRules")

        config = RoomAccessRulesConfig(**config_dict)

        return config

    async def _fix_existing_rooms_task(
        self, task: ScheduledTask, fixer: Callable[[str], Awaitable[None]]
    ) -> None:
        def get_room_ids_from(
            txn: LoggingTransaction,
            limit: Optional[int] = None,
            from_id: Optional[str] = None,
        ) -> List[str]:
            limit_statement = ""
            if limit is not None:
                limit_statement = f"LIMIT {limit}"

            where_statement = ""
            if from_id:
                where_statement = f"WHERE room_id > '{from_id}'"

            txn.execute(
                f"SELECT * FROM rooms {where_statement} ORDER BY room_id {limit_statement}"
            )
            rows = txn.fetchall()
            return [r[0] for r in rows]

        last_room_id = None
        # Let's resume the task from the last processed room
        if task.result:
            last_room_id = task.result.get("last_room_id")

        # Let's iterate on all rooms by pack of 100
        has_next = True
        while has_next:
            room_ids = await self.module_api.run_db_interaction(
                "get_room_ids_from",
                get_room_ids_from,
                limit=50,
                from_id=last_room_id,
            )

            # No more rooms around, let's stop
            if len(room_ids) == 0:
                has_next = False

            for room_id in room_ids:
                await fixer(room_id)
                last_room_id = room_id

            # Update task result so it is resumed from the last
            # fully processed batch of rooms
            # We don't do it for each room for perf reason
            await self.task_scheduler.update_task(
                task.id,
                status=TaskStatus.ACTIVE,
                result={"last_room_id": last_room_id},
            )

    async def get_local_admin_user(
        self, room_id: str, power_levels_event: EventBase
    ) -> str | None:
        # Fetch local users joined to the room
        local_joined_users = set()
        for user_id, membership in await self.store.get_local_users_related_to_room(
            room_id
        ):
            if membership == "join":
                local_joined_users.add(user_id)

        # Check if we have a local admin from power levels event
        if power_levels_event and power_levels_event.content:
            content = unfreeze(power_levels_event.content)
            content.setdefault("users", {})
            for u in content["users"]:
                if u in local_joined_users and content["users"][u] == 100:
                    # if u is in local_joined_users then it is a str for sure
                    return u  # type: ignore[no-any-return]

        return None

    async def fix_room_power_levels(self, room_id: str) -> None:
        current_state = await self.module_api.get_room_state(
            room_id, [(ACCESS_RULES_TYPE, ""), (EventTypes.PowerLevels, "")]
        )

        power_levels_event = current_state.get((EventTypes.PowerLevels, ""))
        if not power_levels_event:
            logger.info("No power levels event found for room %s, skipping", room_id)
            return
        content = unfreeze(power_levels_event.content)

        local_admin_user = await self.get_local_admin_user(room_id, power_levels_event)
        if local_admin_user:
            # We have an admin on this server !!
            # Let's patch the power levels with it
            changed = False

            if self.config.add_live_location_power_levels:
                # Set location live share needed pl to default events pl
                default_events_pl = content.get("events_default", 0)
                if content["events"].get(LOCATION_LIVE_SHARE_TYPE, None) is None:
                    content["events"][LOCATION_LIVE_SHARE_TYPE] = default_events_pl
                    changed = True
                if content["events"].get(LOCATION_LIVE_SHARE_MSC_TYPE, None) is None:
                    content["events"][LOCATION_LIVE_SHARE_MSC_TYPE] = default_events_pl
                    changed = True

            if self.config.add_matrix_rtc_call_power_levels:
                # Set Matrix RTC Call needed pl to default events pl
                default_events_pl = content.get("events_default", 0)
                existing_pl = content["events"].get(MATRIX_RTC_CALL_MEMBER_TYPE, None)
                if existing_pl is None or existing_pl != default_events_pl:
                    content["events"][MATRIX_RTC_CALL_MEMBER_TYPE] = default_events_pl
                    changed = True
                existing_pl = content["events"].get(
                    MATRIX_RTC_CALL_MEMBER_MSC_TYPE, None
                )
                if existing_pl is None or existing_pl != default_events_pl:
                    content["events"][
                        MATRIX_RTC_CALL_MEMBER_MSC_TYPE
                    ] = default_events_pl
                    changed = True

            if self.config.fix_admins_for_dm_power_levels:
                is_dm = False

                access_rules_event = current_state.get((ACCESS_RULES_TYPE, ""))
                if access_rules_event:
                    if access_rules_event.content.get("rule", None) == "direct":
                        is_dm = True

                if is_dm:
                    # it's a DM, let's try to fix it by putting everyone admins
                    members_state = await self.module_api.get_room_state(
                        room_id, [("m.room.member", None)]
                    )
                    for _, member in members_state:
                        if content["users"].get(member) != 100:
                            content["users"][member] = 100
                            changed = True

            # Send the updated pl event to the room with a local admin
            if changed:
                logger.info(f"Fixing power levels of room {room_id}")
                try:
                    await self.module_api.create_and_send_event_into_room(
                        {
                            "room_id": room_id,
                            "type": EventTypes.PowerLevels,
                            "state_key": "",
                            "sender": local_admin_user,
                            "content": content,
                        }
                    )
                except SynapseError as e:
                    logger.info(
                        f"Not possible to change power levels of room {room_id}, {str(e)}"
                    )
                    logger.debug(content)

    async def fix_existing_rooms_power_levels(
        self, task: ScheduledTask
    ) -> Tuple[TaskStatus, Optional[JsonMapping], Optional[str]]:
        await self._fix_existing_rooms_task(task, self.fix_room_power_levels)

        logger.info("Fixing power levels of existing rooms complete !")

        return TaskStatus.COMPLETE, None, None

    async def fix_visibility_access_rules(self, room_id: str) -> None:
        # We only want to add visibility=public to public rooms, since private is considered
        # the default, so let's check if the room is present in the public room dir of a server.
        if room_id not in self.public_room_ids:
            return

        current_state = await self.module_api.get_room_state(
            room_id,
            [
                (ACCESS_RULES_TYPE, ""),
                (EventTypes.JoinRules, ""),
                (EventTypes.PowerLevels, ""),
            ],
        )

        access_rule_event = current_state.get((ACCESS_RULES_TYPE, ""))
        access_rule_event_visibility = None
        if access_rule_event:
            access_rule_event_visibility = access_rule_event.content.get("visibility")

        join_rule_event = current_state.get((EventTypes.JoinRules, ""))
        if join_rule_event:
            join_rule = join_rule_event.get("content", {}).get("join_rule")
            if (
                join_rule == JoinRules.PUBLIC
                and access_rule_event_visibility != Visibility.PUBLIC
            ):
                # On the principle this could be a "private" room with a link,
                # but we already checked against the public room dir in the first step
                access_rule_content = {}
                if access_rule_event:
                    access_rule_content = unfreeze(access_rule_event.content)

                access_rule_content["visibility"] = Visibility.PUBLIC

                power_levels_event = current_state.get((EventTypes.PowerLevels, ""))
                if not power_levels_event:
                    logger.warning(
                        f"Couldn't fix room visibility for room {room_id}, no power levels event"
                    )
                    return
                local_admin_user = await self.get_local_admin_user(
                    room_id, power_levels_event
                )

                if local_admin_user:
                    logger.info(
                        f"Fixing room visibility in access rules event for room {room_id}"
                    )
                    try:
                        await self.module_api.create_and_send_event_into_room(
                            {
                                "room_id": room_id,
                                "type": ACCESS_RULES_TYPE,
                                "state_key": "",
                                "sender": local_admin_user,
                                "content": access_rule_content,
                            }
                        )
                    except SynapseError as e:
                        logger.warning(
                            f"Not possible to change access rules event of room {room_id}, {str(e)}"
                        )
                        logger.debug(access_rule_content)
                else:
                    logger.warning(
                        f"Couldn't fix room visibility for room {room_id}, no local admin"
                    )

    async def fix_existing_rooms_visibility_access_rules(
        self, task: ScheduledTask
    ) -> Tuple[TaskStatus, Optional[JsonMapping], Optional[str]]:
        # Let's gather all the public rooms by listing all rooms in the public room dir
        # of all servers of the federation.
        self.public_room_ids = set()

        federation_server_names = []
        federation_domain_whitelist = (
            self.module_api._hs.config.federation.federation_domain_whitelist
        )

        if federation_domain_whitelist:
            federation_server_names = list(federation_domain_whitelist.keys())

        for server_name in federation_server_names:
            if server_name == self.module_api.server_name:
                for (
                    room_id
                ) in (
                    await self.module_api._hs.get_storage_controllers().main.get_public_room_ids()
                ):
                    self.public_room_ids.add(room_id)
            else:
                since_token = None
                while True:
                    res = await self.module_api._hs.get_federation_client().get_public_rooms(
                        server_name, since_token=since_token
                    )
                    if "chunk" in res:
                        for r in res["chunk"]:
                            self.public_room_ids.add(r["room_id"])
                    since_token = res.get("next_batch")
                    if not since_token:
                        break

        logger.info(
            f"{len(self.public_room_ids)} public rooms were retrieved in the whole federation"
        )

        await self._fix_existing_rooms_task(task, self.fix_visibility_access_rules)

        logger.info(
            "Fixing visibility attribute on access rules event of existing rooms complete !"
        )

        return TaskStatus.COMPLETE, None, None

    async def on_create_room(
        self,
        requester: Requester,
        config: Dict[str, Any],
        is_requester_admin: bool,
    ) -> bool:
        """
        Checks if requester has permission to create room (external users can not create room)

        Checks if a im.vector.room.access_rules event is being set during room
        creation. If yes, make sure the event is correct. Otherwise, append an event
        with the default rule to the initial state.

        Checks if a m.rooms.power_levels event is being set during room creation. If
        so, make sure the event is allowed. Otherwise, set power_level_content_override
        in the config dict to our modified version of the default room power levels.

        Args:
            requester: The user who is making the createRoom request.
            config: The createRoom config dict provided by the user.
            is_requester_admin: Whether the requester is a Synapse admin.

        Returns:
            Whether the request is allowed.

        Raises:
            SynapseError: If the createRoom config dict is invalid or its contents blocked.
        """
        is_direct = config.get("is_direct")
        preset = config.get("preset")
        visibility = config.get("visibility", Visibility.PRIVATE)
        access_rule = None
        force_unencrypted_at_creation = None
        join_rule = None

        if (
            is_requester_admin
            or requester.user.to_string() in self.config.bypass_for_users
        ):
            return True

        # external users are not allowed to create room
        if get_domain_from_id(requester.user.to_string()) in self.config.domains_forbidden_when_restricted:
            raise SynapseError(403, "Room creation is not allowed for users from external servers (forbidden domains)")

        # Let's use a state map instead of directly manipulating an array,
        # it's less error prone
        initial_state = create_state_map(config.get("initial_state", []))

        encrypted_event = initial_state.get((EventTypes.RoomEncryption, ""))

        # If there's a rules event in the initial state, check if it complies with the
        # spec for im.vector.room.access_rules and deny the request if not.

        access_rule_event = initial_state.get((ACCESS_RULES_TYPE, ""))
        if access_rule_event:
            access_rule = access_rule_event.get("content", {}).get("rule")
            force_unencrypted_at_creation = access_rule_event.get("content", {}).get(
                "force_unencrypted_at_creation"
            )
            access_rule_event_visibility = access_rule_event.get("content", {}).get(
                "visibility"
            )

            # Make sure the event has a valid content.
            if access_rule is None:
                raise SynapseError(400, "Invalid access rule")

            # Make sure the rule name is valid.
            if access_rule not in VALID_ACCESS_RULES:
                raise SynapseError(400, "Invalid access rule")

            if (is_direct and access_rule != AccessRules.DIRECT) or (
                access_rule == AccessRules.DIRECT and not is_direct
            ):
                raise SynapseError(400, "Invalid access rule")

            if (
                access_rule_event_visibility is not None
                and access_rule_event_visibility != visibility
            ):
                raise SynapseError(400, "Incompatible visibility")
        else:
            # If there's no access rules event in the initial state, create one with the
            # default setting.
            if is_direct:
                access_rule = AccessRules.DIRECT
            else:
                # If the default value for non-direct chat changes, we should make another
                # case here for rooms created with either a "public" join_rule or the
                # "public_chat" preset to make sure those keep defaulting to "restricted"
                access_rule = AccessRules.RESTRICTED

            access_rule_event = {
                "type": ACCESS_RULES_TYPE,
                "state_key": "",
                "content": {"rule": access_rule},
            }
            initial_state[(ACCESS_RULES_TYPE, "")] = access_rule_event

        join_rule_event = initial_state.get((EventTypes.JoinRules, ""))
        if join_rule_event:
            join_rule = join_rule_event.get("content", {}).get("join_rule")

        # Check that the preset in use is compatible with the access rule, whether it's
        # user-defined or the default.
        #
        # Direct rooms may not have their join_rules set to JoinRules.PUBLIC.
        if (
            join_rule == JoinRules.PUBLIC or preset == RoomCreationPreset.PUBLIC_CHAT
        ) and access_rule == AccessRules.DIRECT:
            raise SynapseError(400, "Invalid access rule")

        # We need to take care of enforcing encryption in the module:
        # we want to be able to have invite-only unencrypted room, which is not possible
        # when using setting `encryption_enabled_by_default_for_room_type` of synapse
        force_encryption = True
        if join_rule == JoinRules.PUBLIC or preset == RoomCreationPreset.PUBLIC_CHAT:
            force_encryption = False

        if (
            preset == RoomCreationPreset.PRIVATE_CHAT
            and force_unencrypted_at_creation is True
        ):
            force_encryption = False

        if force_encryption and encrypted_event is None:
            initial_state[(EventTypes.RoomEncryption, "")] = {
                "type": EventTypes.RoomEncryption,
                "state_key": "",
                "content": {"algorithm": RoomEncryptionAlgorithms.MEGOLM_V1_AES_SHA2},
            }

        # Set the visibility of the room in the access rules event, to be able to
        # differentiate between public rooms and private rooms with a shareable link.
        initial_state[(ACCESS_RULES_TYPE, "")]["content"]["visibility"] = visibility

        default_power_levels = self._get_default_power_levels(
            requester.user.to_string(), preset
        )

        # This preset should put all invitees as admin, so do it
        if preset == RoomCreationPreset.TRUSTED_PRIVATE_CHAT:
            for invitee in config.get("invite", []):
                default_power_levels["users"][invitee] = 100

        # Check if the creator can override values for the power levels.
        allowed = self._is_power_level_content_allowed(
            config.get("power_level_content_override", {}),
            access_rule,
            default_power_levels,
        )
        if not allowed:
            raise SynapseError(400, "Invalid power levels content override")

        custom_user_power_levels = config.get("power_level_content_override")

        pl_event = initial_state.get((EventTypes.PowerLevels, ""))
        if pl_event:
            allowed = self._is_power_level_content_allowed(
                pl_event["content"], access_rule, default_power_levels
            )
            if not allowed:
                raise SynapseError(400, "Invalid power levels content")

            custom_user_power_levels = pl_event["content"]

        if custom_user_power_levels:
            # If the user is using their own power levels, but failed to provide an
            # expected key in the power levels content dictionary, fill it in from the
            # defaults instead
            self._fill_power_levels(custom_user_power_levels, default_power_levels)
        else:
            # If power levels were not overridden by the user, completely override with
            # the defaults instead
            config["power_level_content_override"] = default_power_levels

        # If an history visibility is not specified, defaults to invited for private rooms
        # and shared for public ones
        if initial_state.get((EventTypes.RoomHistoryVisibility, "")) is None:
            history_visibility = "invited"
            if (
                join_rule == JoinRules.PUBLIC
                or preset == RoomCreationPreset.PUBLIC_CHAT
            ):
                history_visibility = "shared"

            initial_state[(EventTypes.RoomHistoryVisibility, "")] = {
                "type": EventTypes.RoomHistoryVisibility,
                "state_key": "",
                "content": {"history_visibility": history_visibility},
            }

        config["initial_state"] = initial_state.values()

        return True

    @staticmethod
    def _fill_power_levels(
        custom_user_power_levels: Dict[Any, Any],
        default_power_levels: Mapping[Any, Any],
    ) -> None:
        """Recursively fill in missing keys in custom_user_power_levels from
        default_power_levels.
        """
        for key, value in default_power_levels.items():
            if isinstance(value, dict):
                RoomAccessRules._fill_power_levels(
                    custom_user_power_levels.setdefault(key, {}), value
                )
            else:
                custom_user_power_levels.setdefault(key, value)

    # If power levels are not overridden by the user during room creation, the following
    # rules are used instead. Changes from Synapse's default power levels are noted.
    #
    # The same power levels are currently applied regardless of room preset.
    @staticmethod
    def _get_default_power_levels(
        user_id: str,
        room_creation_preset: str | None = None,
    ) -> Dict[str, Any]:
        if room_creation_preset is None:
            room_creation_preset = RoomCreationPreset.PRIVATE_CHAT
        return {
            "users": {user_id: 100},
            "users_default": 0,
            "events": {
                EventTypes.Name: 50,
                EventTypes.PowerLevels: 100,
                EventTypes.RoomHistoryVisibility: 100,
                EventTypes.CanonicalAlias: 50,
                EventTypes.RoomAvatar: 50,
                EventTypes.Tombstone: 100,
                EventTypes.ServerACL: 100,
                EventTypes.RoomEncryption: 100,
                # We want normal users to be able to use live location sharing by default
                LOCATION_LIVE_SHARE_TYPE: 0,
                LOCATION_LIVE_SHARE_MSC_TYPE: 0,
                # We want normal users to be able to particpate in Matrix RTC calls by default
                MATRIX_RTC_CALL_MEMBER_TYPE: 0,
                MATRIX_RTC_CALL_MEMBER_MSC_TYPE: 0,
            },
            "events_default": 0,
            "state_default": 100,  # Admins should be the only ones to perform other tasks
            "ban": 50,
            "kick": 50,
            "redact": 50,
            "invite": (
                0 if room_creation_preset == "public_chat" else 50
            ),  # Public room allows invite for all whereas other rooms should require mod to invite
        }

    async def user_may_send_3pid_invite(
        self,
        inviter: str,
        medium: str,
        address: str,
        room_id: str,
    ) -> Literal["NOT_SPAM"] | Codes:
        """Check if a threepid can be invited to the room via a 3PID invite given the
        current rules and the threepid's address, by retrieving the HS it's mapped to
        from the configured identity server, and checking if we can invite users from it.

        Args:
            inviter: The user ID of the inviter.
            medium: The medium of the threepid.
            address: The address of the threepid.
            room_id: The ID of the room to check.

        Returns:
            Whether the threepid invite is allowed.
        """
        state_events = await self.module_api.get_room_state(room_id)
        rule = self._get_rule_from_state(state_events)

        if medium != "email":
            return Codes.FORBIDDEN

        if rule != AccessRules.RESTRICTED:
            # Only "restricted" requires filtering 3PID invites. We don't need to do
            # anything for "direct" here, because only "restricted" requires filtering
            # based on the HS the address is mapped to.
            return "NOT_SPAM"

        parsed_address = email.utils.parseaddr(address)[1]
        if parsed_address != address:
            # Avoid reproducing the security issue described here:
            # https://matrix.org/blog/2019/04/18/security-update-sydent-1-0-2
            # It's probably not worth it but let's just be overly safe here.
            return Codes.FORBIDDEN

        # Get the HS this address belongs to from the identity server.
        res = await self.module_api.http_client.get_json(
            "https://%s/_matrix/identity/api/v1/info" % (self.config.id_server,),
            {"medium": medium, "address": address},
        )

        # Look for a domain that's not forbidden from being invited.
        if not res.get("hs"):
            return Codes.FORBIDDEN
        if res.get("hs") in self.config.domains_forbidden_when_restricted:
            return Codes.FORBIDDEN

        return "NOT_SPAM"

    async def _user_can_bypass_rules(self, user_id: str) -> bool:
        if (
            user_id in self.config.bypass_for_users
            or await self.module_api.is_user_admin(user_id)
        ):
            return True
        return False

    async def check_event_for_spam(
        self,
        event: EventBase,
    ) -> Literal["NOT_SPAM"] | Codes:
        state_ids = await self.storage_controllers.state.get_current_state_ids(
            event.room_id,
            await_full_state=False,
        )
        events = await self.store.get_events(state_ids.values())
        state_events: StateMap[EventBase] = {
            key: events[event_id] for key, event_id in state_ids.items()
        }

        if await self._check_event_allowed(event, state_events):
            return "NOT_SPAM"
        return Codes.FORBIDDEN

    async def _check_event_allowed(
        self,
        event: EventBase,
        state_events: StateMap[EventBase],
    ) -> bool:
        """Checks the event's type and the current rule and calls the right function to
        determine whether the event can be allowed.

        Args:
            event: The event to check.
            state_events: A dict mapping (event type, state key) to state event.
                State events in the room the event originated from.

        Returns:
            A 2-tuple (allowed, None). `allowed` is True if the event should be
            allowed, False if it should be rejected. The second entry is always
            None because this module doesn't replace event contents.
        """
        if await self._user_can_bypass_rules(event.sender):
            return True

        # We check the rules when altering the state of the room, so only go further if
        # the event is a state event.
        if event.is_state():
            if event.type == ACCESS_RULES_TYPE:
                return await self._on_access_rules_event_change(event, state_events)

            # We need to know the rule to apply when processing the event types below.
            rule = self._get_rule_from_state(state_events)

            if event.type == EventTypes.PowerLevels:
                return self._is_power_level_content_allowed(
                    event.content, rule, on_room_creation=False
                )

            if (
                event.type == EventTypes.Member
                or event.type == EventTypes.ThirdPartyInvite
            ):
                return await self._on_membership_or_invite(event, rule, state_events)

            if event.type == EventTypes.JoinRules:
                return self._on_join_rule_change(event, rule, state_events)

            if event.type == EventTypes.RoomAvatar:
                return self._on_room_avatar_change(event, rule)

            if event.type == EventTypes.Name:
                return self._on_room_name_change(event, rule)

            if event.type == EventTypes.Topic:
                return self._on_room_topic_change(event, rule)

            if event.type == EventTypes.RoomEncryption:
                return self._on_room_encryption_change(event, state_events)

        return True

    async def check_visibility_can_be_modified(
        self, room_id: str, state_events: StateMap[EventBase], new_visibility: str
    ) -> bool:
        """Determines whether a room can be published, or removed from, the public room
        list. A room is published if its visibility is set to "public". Otherwise,
        its visibility is "private". A room with access rule other than "restricted"
        may not be published.

        Args:
            room_id: The ID of the room.
            state_events: A dict mapping (event type, state key) to state event.
                State events in the room.
            new_visibility: The new visibility state. Either "public" or "private".

        Returns:
            Whether the room is allowed to be published to, or removed from, the public
            rooms directory.
        """
        # We need to know the rule to apply when processing the event types below.
        rule = self._get_rule_from_state(state_events)

        # Allow adding a room to the public rooms list only if it is restricted
        if new_visibility == Visibility.PUBLIC:
            return rule == AccessRules.RESTRICTED

        # By default a room is created as "restricted", meaning it is allowed to be
        # published to the public rooms directory.
        return True

    def _check_rule(self, event: EventBase, state_events: StateMap[EventBase]) -> bool:
        new_rule = event.content.get("rule")

        # TODO should we allow empty rule values?
        # Check for invalid values.
        if new_rule not in VALID_ACCESS_RULES:
            return False

        # Make sure we don't apply "direct" if the room has more than two members.
        if new_rule == AccessRules.DIRECT:
            existing_members, threepid_tokens = self._get_members_and_tokens_from_state(
                state_events
            )

            if len(existing_members) > 2 or len(threepid_tokens) > 1:
                return False

        prev_rules_event = state_events.get((ACCESS_RULES_TYPE, ""))
        # Now that we know the new rule doesn't break the "direct" case, we can allow any
        # new rule in rooms that had none before and is not public.
        # (public rooms have visibility=public in a rules event)
        if prev_rules_event is None:
            return True

        prev_rule = prev_rules_event.content.get("rule")
        visibility = prev_rules_event.content.get("visibility", "private")

        if prev_rule == new_rule:
            return True

        # Block opening this room to external users if this room is marked as a public room
        if new_rule != AccessRules.RESTRICTED and visibility == Visibility.PUBLIC:
            return False

        # We don't want to be able to forbid a room to external users after they have been allowed
        # because some could already have joined.
        return (
            prev_rule == AccessRules.RESTRICTED and new_rule == AccessRules.UNRESTRICTED
        )

    def _check_visibility(
        self,
        event: EventBase,
        state_events: StateMap[EventBase],
        is_local_event: bool,
    ) -> bool:
        new_visibility = event.content.get("visibility", "private")
        prev_rules_event = state_events.get((ACCESS_RULES_TYPE, ""))

        if prev_rules_event:
            # The meaning of the visibility parameter should never be changed after creation of the room.

            current_visibility = prev_rules_event.content.get("visibility", "private")
            # deny current_visibility updates unless when fix_visibility_access_rules is active
            if (
                new_visibility != current_visibility
                and not self.config.fix_visibility_access_rules
            ):
                return False
        elif is_local_event and new_visibility != "private":
            # For local events, set a visibility other than "private" is only allowed at room creation
            # and this is handled in `on_create_room`
            return False

        return True

    def _check_force_unencrypted_at_creation(
        self,
        event: EventBase,
        state_events: StateMap[EventBase],
        is_local_event: bool,
    ) -> bool:
        prev_rules_event = state_events.get((ACCESS_RULES_TYPE, ""))

        new_force_unencrypted = event.content.get(
            "force_unencrypted_at_creation", False
        )
        if prev_rules_event:
            # This value should never be changed after creation of the room.
            current_force_unencrypted = prev_rules_event.content.get(
                "force_unencrypted_at_creation", False
            )
            if new_force_unencrypted != current_force_unencrypted:
                return False
        elif is_local_event and new_force_unencrypted is not False:
            # For local events, set force_unencrypted_at_creation=true is only allowed at room creation
            # and this is handled in `on_create_room`
            return False

        return True

    async def _on_access_rules_event_change(
        self, event: EventBase, state_events: StateMap[EventBase]
    ) -> bool:
        """Checks whether an im.vector.room.access_rules event is forbidden or allowed.

        Args:
            event: The im.vector.room.access_rules event.
            state_events: A dict mapping (event type, state key) to state event.
                State events in the room before the event was sent.
        Returns:
            True if the event can be allowed, False otherwise.
        """
        is_local_event = (
            UserID.from_string(event.sender).domain == self.module_api.server_name
        )

        if not self._check_rule(event, state_events):
            return False

        if not self._check_force_unencrypted_at_creation(
            event, state_events, is_local_event
        ):
            return False

        if not self._check_visibility(event, state_events, is_local_event):
            return False

        return True

    async def _on_membership_or_invite(
        self,
        event: EventBase,
        rule: str,
        state_events: StateMap[EventBase],
    ) -> bool:
        """Applies the correct rule for incoming m.room.member and
        m.room.third_party_invite events.

        Args:
            event: The event to check.
            rule: The name of the rule to apply.
            state_events: A dict mapping (event type, state key) to state event.
                The state of the room before the event was sent.

        Returns:
            A boolean indicating whether the event is allowed.
        """

        # Let's ignore rules if the user is accepting an invite coming from
        # an user in the bypass list or an admin
        if event.type == EventTypes.Member and event.membership == Membership.JOIN:
            previous_membership = state_events.get((EventTypes.Member, event.state_key))
            if (
                previous_membership
                and previous_membership.membership == Membership.INVITE
            ):
                if await self._user_can_bypass_rules(previous_membership.sender):
                    return True

        # Let's ignore rules if the invited user is in the bypass list or an admin
        if (
            event.type == EventTypes.Member
            and event.membership == Membership.INVITE
            and await self._user_can_bypass_rules(event.state_key)
        ):
            return True

        if rule == AccessRules.RESTRICTED:
            ret = self._on_membership_or_invite_restricted(event)
        elif rule == AccessRules.UNRESTRICTED:
            ret = self._on_membership_or_invite_unrestricted(event, state_events)
        elif rule == AccessRules.DIRECT:
            ret = self._on_membership_or_invite_direct(event, state_events)
        else:
            # We currently apply the default (restricted) if we don't know the rule, we
            # might want to change that in the future.
            ret = self._on_membership_or_invite_restricted(event)

        return ret

    def _on_membership_or_invite_restricted(self, event: EventBase) -> bool:
        """Implements the checks and behaviour specified for the "restricted" rule.

        "restricted" currently means that users can only invite users if their server is
        included in a limited list of domains.

        Args:
            event: The event to check.

        Returns:
            True if the event can be allowed, False otherwise.
        """
        # We're not applying the rules on m.room.third_party_member events here because
        # the filtering on threepids is done in check_threepid_can_be_invited, which is
        # called before check_event_allowed.
        if event.type == EventTypes.ThirdPartyInvite:
            return True

        # We only need to process "join" and "invite" memberships, in order to be backward
        # compatible, e.g. if a user from a blacklisted server joined a restricted room
        # before the rules started being enforced on the server, that user must be able to
        # leave it.
        if event.membership not in [Membership.JOIN, Membership.INVITE]:
            return True

        invitee_domain = UserID.from_string(event.state_key).domain
        return invitee_domain not in self.config.domains_forbidden_when_restricted

    def _on_membership_or_invite_unrestricted(
        self, event: EventBase, state_events: StateMap[EventBase]
    ) -> bool:
        """Implements the checks and behaviour specified for the "unrestricted" rule.

        "unrestricted" currently means that forbidden users cannot join without an invite.

        Returns:
            True if the event can be allowed, False otherwise.
        """
        # If this is a join from a forbidden user and they don't have an invite to the
        # room, then deny it
        if event.type == EventTypes.Member and event.membership == Membership.JOIN:
            # Check if this user is from a forbidden server
            target_domain = UserID.from_string(event.state_key).domain
            if target_domain in self.config.domains_forbidden_when_restricted:
                # If so, they'll need an invite to join this room. Check if one exists
                if not self._user_is_invited_to_room(event.state_key, state_events):
                    return False

        return True

    def _on_membership_or_invite_direct(
        self,
        event: EventBase,
        state_events: StateMap[EventBase],
    ) -> bool:
        """Implements the checks and behaviour specified for the "direct" rule.

        "direct" currently means that no member is allowed apart from the two initial
        members the room was created for (i.e. the room's creator and their first
        invitee).

        Args:
            event: The event to check.
            state_events: A dict mapping (event type, state key) to state event.
                The state of the room before the event was sent.

        Returns:
            True if the event can be allowed, False otherwise.
        """
        # Get the room memberships and 3PID invite tokens from the room's state.
        existing_members, threepid_tokens = self._get_members_and_tokens_from_state(
            state_events
        )

        # There should never be more than one 3PID invite in the room state: if the second
        # original user came and left, and we're inviting them using their email address,
        # given we know they have a Matrix account binded to the address (so they could
        # join the first time), Synapse will successfully look it up before attempting to
        # store an invite on the IS.
        if len(threepid_tokens) == 1 and event.type == EventTypes.ThirdPartyInvite:
            # If we already have a 3PID invite in flight, don't accept another one, unless
            # the new one has the same invite token as its state key. This is because 3PID
            # invite revocations must be allowed, and a revocation is basically a new 3PID
            # invite event with an empty content and the same token as the invite it
            # revokes.
            return event.state_key in threepid_tokens

        if len(existing_members) == 2:
            # If the user was within the two initial user of the room, Synapse would have
            # looked it up successfully and thus sent a m.room.member here instead of
            # m.room.third_party_invite.
            if event.type == EventTypes.ThirdPartyInvite:
                return False

            # We can only have m.room.member events here. The rule in this case is to only
            # allow the event if its target is one of the initial two members in the room,
            # i.e. the state key of one of the two m.room.member states in the room.
            return event.state_key in existing_members

        # We're alone in the room (and always have been) and there's one 3PID invite in
        # flight.
        if len(existing_members) == 1 and len(threepid_tokens) == 1:
            # We can only have m.room.member events here. In this case, we can only allow
            # the event if it's either a m.room.member from the joined user (we can assume
            # that the only m.room.member event is a join otherwise we wouldn't be able to
            # send an event to the room) or an an invite event which target is the invited
            # user.
            target = event.state_key
            is_from_threepid_invite = self._is_invite_from_threepid(
                event, threepid_tokens[0]
            )
            return is_from_threepid_invite or target == existing_members[0]

        return True

    def _is_power_level_content_allowed(
        self,
        content: Dict[str, Any],
        access_rule: str,
        default_power_levels: Optional[Dict[str, Any]] = None,
        on_room_creation: bool = True,
    ) -> bool:
        """Check if a given power levels event is permitted under the given access rule.

        It shouldn't be allowed if it either changes the default PL to a non-0 value or
        gives a non-0 PL to a user that would have been forbidden from joining the room
        under a more restrictive access rule.

        Args:
            content: The content of the m.room.power_levels event to check.
            access_rule: The access rule in place in this room.
            default_power_levels: The default power levels when a room is created with
                the specified access rule. Required if on_room_creation is True.
            on_room_creation: True if this call is happening during a room's
                creation, False otherwise.

        Returns:
            Whether the content of the power levels event is valid.
        """
        # Only enforce these rules during room creation
        #
        # We want to allow admins to modify or fix the power levels in a room if they
        # have a special circumstance, but still want to encourage a certain pattern
        # during room creation.
        if on_room_creation and default_power_levels:
            # We specifically don't fail if "invite" or "state_default" are None, as those
            # values should be replaced with our "default" power level values anyways,
            # which are compliant

            invite = default_power_levels["invite"]
            state_default = default_power_levels["state_default"]

            # If invite requirements are less than our required defaults
            if content.get("invite", invite) < invite:
                return False

            # If "other" state requirements are less than our required defaults
            if content.get("state_default", state_default) < state_default:
                return False

        # Check if we need to apply the restrictions with the current rule.
        if access_rule not in RULES_WITH_RESTRICTED_POWER_LEVELS:
            return True

        # If users_default is explicitly set to a non-0 value, deny the event.
        users_default = content.get("users_default", 0)
        if users_default:
            return False

        users = content.get("users", {})
        for user_id, power_level in users.items():
            server_name = UserID.from_string(user_id).domain
            # Check the domain against the blacklist. If found, and the PL isn't 0, deny
            # the event.
            if (
                server_name in self.config.domains_forbidden_when_restricted
                and power_level != 0
            ):
                return False

        return True

    def _on_join_rule_change(
        self, event: EventBase, rule: str, state_events: StateMap[EventBase]
    ) -> bool:
        """Check whether a join rule change is allowed.

        A join rule change is always allowed unless:
        - the new join rule is "public" and the current access rule is "direct". We need to
          allow public join rules for private rooms too to be able to have rooms joinable by link.
        - the existing join rule is "public" and the room has public visibility (it's a forum)

        Args:
            event: The event to check.
            rule: The name of the rule to apply.

        Returns:
            Whether the change is allowed.
        """
        if event.content.get("join_rule") == JoinRules.PUBLIC:
            return rule != AccessRules.DIRECT

        visibility = Visibility.PRIVATE
        access_rules_event = state_events.get((ACCESS_RULES_TYPE, ""))
        if access_rules_event:
            visibility = access_rules_event.content.get(
                "visibility", Visibility.PRIVATE
            )

        if (
            self._get_join_rule_from_state(state_events) == JoinRules.PUBLIC
            and event.content.get("join_rule") != JoinRules.PUBLIC
        ):
            if visibility == Visibility.PUBLIC:
                return False

        return True

    def _on_room_avatar_change(self, event: EventBase, rule: str) -> bool:
        """Check whether a change of room avatar is allowed.
        The current rule is to forbid such a change in direct chats but allow it
        everywhere else.

        Args:
            event: The event to check.
            rule: The name of the rule to apply.

        Returns:
            True if the event can be allowed, False otherwise.
        """
        return rule != AccessRules.DIRECT

    def _on_room_name_change(self, event: EventBase, rule: str) -> bool:
        """Check whether a change of room name is allowed.
        The current rule is to forbid such a change in direct chats but allow it
        everywhere else.

        Args:
            event: The event to check.
            rule: The name of the rule to apply.

        Returns:
            True if the event can be allowed, False otherwise.
        """
        return rule != AccessRules.DIRECT

    def _on_room_topic_change(self, event: EventBase, rule: str) -> bool:
        """Check whether a change of room topic is allowed.
        The current rule is to forbid such a change in direct chats but allow it
        everywhere else.

        Args:
            event: The event to check.
            rule: The name of the rule to apply.

        Returns:
            True if the event can be allowed, False otherwise.
        """
        return rule != AccessRules.DIRECT

    def _on_room_encryption_change(
        self, event: EventBase, state_events: StateMap[EventBase]
    ) -> bool:
        """Check whether a room can have its encryption enabled.
        The current rule is to forbid such a change in public rooms or in unencrypted private room

        Args:
            event: The event to check.
            state_events: A dict mapping (event type, state key) to state event.

        Returns:
            True if the event can be allowed, False otherwise.
        """

        visibility = Visibility.PRIVATE
        force_unencrypted_at_creation = False
        access_rules_event = state_events.get((ACCESS_RULES_TYPE, ""))
        if access_rules_event:
            visibility = access_rules_event.content.get(
                "visibility", Visibility.PRIVATE
            )
            force_unencrypted_at_creation = access_rules_event.content.get(
                "force_unencrypted_at_creation", False
            )
        return not force_unencrypted_at_creation and visibility != Visibility.PUBLIC

    @staticmethod
    def _get_rule_from_state(state_events: StateMap[EventBase]) -> str:
        """Extract the rule to be applied from the given set of state events.

        Args:
            state_events: A dict mapping (event type, state key) to state event.

        Returns:
            The name of the rule (either "direct", "restricted" or "unrestricted") if
            found, else "restricted".
        """
        access_rules = state_events.get((ACCESS_RULES_TYPE, ""))
        if access_rules is None:
            return AccessRules.RESTRICTED

        return access_rules.content.get("rule") or AccessRules.RESTRICTED

    @staticmethod
    def _get_join_rule_from_state(state_events: StateMap[EventBase]) -> Optional[str]:
        """Extract the room's join rule from the given set of state events.

        Args:
            state_events (dict[tuple[event type, state key], EventBase]): The set of state
                events.

        Returns:
            The name of the join rule (either "public", or "invite") if found, else None.
        """
        join_rule_event = state_events.get((EventTypes.JoinRules, ""))
        if join_rule_event is None:
            return None

        return join_rule_event.content.get("join_rule")

    @staticmethod
    def _get_members_and_tokens_from_state(
        state_events: StateMap[EventBase],
    ) -> Tuple[List[str], List[str]]:
        """Retrieves the list of users that have a m.room.member event in the room,
        as well as 3PID invites tokens in the room.

        Args:
            state_events: A dict mapping (event type, state key) to state event.

        Returns:
            A tuple containing the:
                * targets of the m.room.member events in the state.
                * 3PID invite tokens in the state.
        """
        existing_members = []
        threepid_invite_tokens = []
        for key, state_event in state_events.items():
            if key[0] == EventTypes.Member and state_event.content:
                existing_members.append(state_event.state_key)
            if key[0] == EventTypes.ThirdPartyInvite and state_event.content:
                # Don't include revoked invites.
                threepid_invite_tokens.append(state_event.state_key)

        return existing_members, threepid_invite_tokens

    @staticmethod
    def _is_invite_from_threepid(invite: EventBase, threepid_invite_token: str) -> bool:
        """Checks whether the given invite follows the given 3PID invite.

        Args:
             invite: The m.room.member event with "invite" membership.
             threepid_invite_token: The state key from the 3PID invite.

        Returns:
            Whether the invite is due to the given 3PID invite.
        """
        token: str = (
            invite.content.get("third_party_invite", {})
            .get("signed", {})
            .get("token", "")
        )

        return token == threepid_invite_token

    def _user_is_invited_to_room(
        self, user_id: str, state_events: StateMap[EventBase]
    ) -> bool:
        """Checks whether a given user has been invited to a room

        A user has an invite for a room if its state contains a `m.room.member`
        event with membership "invite" and their user ID as the state key.

        Args:
            user_id: The user to check.
            state_events: The state events from the room.

        Returns:
            True if the user has been invited to the room, or False if they haven't.
        """
        for (event_type, state_key), state_event in state_events.items():
            if (
                event_type == EventTypes.Member
                and state_key == user_id
                and state_event.membership == Membership.INVITE
            ):
                return True

        return False


def create_state_map(
    initial_state: List[JsonDict] | None,
) -> MutableStateMap[JsonDict]:
    if initial_state is None:
        initial_state = []
    initial_state_map: MutableStateMap[JsonDict] = {}
    for event_dict in initial_state:
        if "type" in event_dict:
            state_key = event_dict.get("state_key", "")
            initial_state_map[(event_dict["type"], state_key)] = event_dict
    return initial_state_map
