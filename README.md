# Room Access rules

This module implements handling around the `im.vector.room.access_rules` state event. A specification for this event is described below.

## `im.vector.room.access_rules`

Restricts the access to a room based on the selected preset. Body:

```json
{
    "rule": "<rule>",
    "visibility": "<visibility>",
    "force_unencrypted_at_creation": <bool>
}
```

* `rule` (required): one of `restricted`, `unrestricted` or `direct`.
* `visibility` (optional): either `public` or `private`. Indicates
  whether the room is listed in the public room directory
* `force_unencrypted_at_creation` (optional, at room creation only): boolean. When set to
  `true` on a room created with the `private_chat` preset, prevents
  the module from forcing end-to-end encryption on the room.

The implementation of the different presets lives in the
`synapse.third_party_rules.access_rules` module.

### `restricted` preset

Default preset for non-direct rooms (i.e. rooms not created with `"is_direct": true`).

Forbids any invite and membership update for users that belong to a server
that is in the blacklist provided by the module's configuration
(`domains_forbidden_when_restricted`). If the invite is a 3PID invite, queries
a custom `/_matrix/identity/api/v1/info` endpoint of the configured identity server to check if the invited email
address belongs to a blacklisted server, in which case the invite is denied.

### `unrestricted` preset

Doesn't apply any restriction on who can join the room.

Forbids any `m.room.power_levels` event that either:

* change the `users_default` power level to a non-0 value, or
* change the power level for a user from a blacklisted server (see details about the `restricted` preset) to a non-default value

### `direct` preset

Default preset for direct rooms (i.e. rooms created with `"is_direct": true`).

Only allow two members in the room by running the following algorithm for
each new event of type `m.room.member` or `m.room.third_party_invite` sent
into the room:

0. retrieve the list of memberships and 3PID invite tokens from the room's state, which in practice means retrieving the state key of every `m.room.member` or `m.room.third_party_invite` event present in the room's state (ignoring 3PID invite events with an empty content)

1. if the event is of type `m.room.third_party_invite`, and there are already events of the same type in the room's state, reject the new event if its state key doesn't match the state key of one of the existing events.

2. else, if there are already two members in the room:

    2.1. if the event is a 3PID invite, reject it

    2.2. if the event is a membership update, reject it if the target isn't one of the room's current members

3. else, if there is one membership event and one 3PID invite in the room's state:

    3.1. if the event is a membership event, reject it if it's not an invite exchanged from the 3PID invite that's in the room's state

    3.2. otherwise, reject the event

4. else, accept the event

Also forbids sending an event of the type `m.room.name`, `m.room.avatar_url`
or `m.room.topic` into the room.

### Force unencryption at room creation

At room creation, the module forces end-to-end encryption unless one of the following is true:

* the room is being created with `join_rule = public` or with the
  `public_chat` preset;
* the room is being created with the `private_chat` preset **and** the
  `im.vector.room.access_rules` event provided in `initial_state`
  explicitly sets `force_unencrypted_at_creation` to `true`.

This allows invite-only unencrypted rooms to be created, which isn't
possible with Synapse's built-in
`encryption_enabled_by_default_for_room_type` setting.

The `force_unencrypted_at_creation` attribute of the `im.vector.room.access_rules` event
is only meaningful at room creation time.

### Room visibility

The module tracks a `visibility` attribute inside the `im.vector.room.access_rules` event, which can be either `public` or `private` (defaults to `private`). This is distinct from `m.room.history_visibility` and is used to tell apart:

* `visibility = public` room is listed in the server's public room directory;
* `visibility = private` room is not listed in the server's public room directory;

The `visibility` is set by the module at room creation from the `visibility` field of the `createRoom` request.

### Interaction with `m.room.join_rules`

When the preset of the room is something other than `restricted`, changing the
room's join rule to `public` is forbidden. This is to ensure
users on blacklisted servers (see details about the `restricted` preset) can't
join a room unless they have been invited.

## Installation

```
pip install synapse-room-access-rules
```

## Config

Add the following to your Synapse config:

```yaml
modules:
  - module: room_access_rules.RoomAccessRules
    config:
        # List of domains (server names) that can't be invited to rooms if the
        # "restricted" rule is set. Defaults to an empty list.
        domains_forbidden_when_restricted: []
    
        # Identity server to use when checking the homeserver an email address belongs to
        # using the /info endpoint. Required.
        id_server: "vector.im"
        # Disable access rules for this list of users
        bypass_for_users: []
```

### Configuration Flags

The following boolean flags can be used to enable automatic fixes (jobs) for existing rooms:

* `fix_admins_for_dm_power_levels` (default: `false`): When enabled, automatically sets all members of direct message rooms as admins (power level 100). This ensures both participants have equal administrative rights. Runs once on startup.

* `add_live_location_power_levels` (default: `false`): When enabled, adds power level configuration for live location sharing events (`m.beacon_info` and `org.matrix.msc3672.beacon_info`) if missing, setting them to the default event power level. This allows normal users to use live location sharing by default. Runs once on startup.

* `add_matrix_rtc_call_power_levels` (default: `false`): When enabled, adds power level configuration for Matrix RTC call events (`m.call.member` and `org.matrix.msc3401.call.member`) if missing, setting them to the default event power level. This allows normal users to participate in calls by default. Runs once on startup.

* `fix_visibility_access_rules` (default: `false`): When enabled, automatically updates the `visibility` attribute in the `im.vector.room.access_rules` event for public rooms that are missing this attribute. Runs once on startup.

## Development and Testing

This repository uses `tox` to run tests.

### Tests

This repository uses `unittest` to run the tests located in the `tests`
directory. They can be ran with `tox -e tests`.

### Making a release

```
git tag -s vX.Y
python3 setup.py sdist
twine upload dist/synapse-room-access-rules-X.Y.tar.gz
git push origin vX.Y
```