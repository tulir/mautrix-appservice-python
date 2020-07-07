# Copyright (c) 2020 Tulir Asokan
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from typing import Optional
import logging

from mautrix.client import Client, InternalEventType, DeviceOTKCount, DeviceLists
from mautrix.types import StateEvent, ToDeviceEvent, Membership, EventType, EncryptionAlgorithm
from mautrix.util.logging import TraceLogger

from .store import CryptoStore, StateStore
from .types import DecryptedOlmEvent
from .account import OlmAccount
from .device_lists import DeviceListMachine
from .encrypt_olm import OlmEncryptionMachine
from .decrypt_olm import OlmDecryptionMachine
from .encrypt_megolm import MegolmEncryptionMachine
from .decrypt_megolm import MegolmDecryptionMachine


class OlmMachine(DeviceListMachine, OlmEncryptionMachine, OlmDecryptionMachine,
                 MegolmEncryptionMachine, MegolmDecryptionMachine):
    client: Client
    log: TraceLogger
    crypto_store: CryptoStore
    state_store: StateStore

    account: OlmAccount

    allow_unverified_devices: bool

    def __init__(self, client: Client, crypto_store: CryptoStore, state_store: StateStore,
                 log: Optional[TraceLogger] = None) -> None:
        self.client = client
        self.log = log or logging.getLogger("mau.crypto")
        self.crypto_store = crypto_store
        self.state_store = state_store

        self.allow_unverified_devices = True

        self.client.add_event_handler(InternalEventType.DEVICE_OTK_COUNT, self.handle_otk_count)
        self.client.add_event_handler(InternalEventType.DEVICE_LISTS, self.handle_device_lists)
        self.client.add_event_handler(EventType.TO_DEVICE_ENCRYPTED, self.handle_to_device_event)
        self.client.add_event_handler(EventType.ROOM_MEMBER, self.handle_member_event)

    async def load(self) -> None:
        self.account = await self.crypto_store.get_account()
        if self.account is None:
            self.account = OlmAccount()

    async def handle_otk_count(self, otk_count: DeviceOTKCount) -> None:
        if otk_count.signed_curve25519 < self.account.max_one_time_keys // 2:
            self.log.trace(f"Sync response said we have {otk_count.signed_curve25519} signed"
                           " curve25519 keys left, sharing new ones...")
            await self.share_keys(otk_count.signed_curve25519)

    async def handle_device_lists(self, device_lists: DeviceLists) -> None:
        if len(device_lists.changed) > 0:
            await self._fetch_keys(device_lists.changed, include_untracked=False)

    async def handle_member_event(self, evt: StateEvent) -> None:
        if not await self.state_store.is_encrypted(evt.room_id):
            return
        prev = evt.prev_content.membership or Membership.UNKNOWN
        cur = evt.content.membership
        ignored_changes = {
            Membership.INVITE: Membership.JOIN,
            Membership.BAN: Membership.LEAVE,
            Membership.LEAVE: Membership.BAN,
        }
        if prev == cur or ignored_changes.get(prev) == cur:
            return
        self.log.trace(f"Got membership state event in {evt.room_id} changing {evt.state_key} from "
                       f"{prev} to {cur}, invalidating group session")
        await self.crypto_store.remove_outbound_group_session(evt.room_id)

    async def handle_to_device_event(self, evt: ToDeviceEvent) -> None:
        self.log.trace(f"Handling encrypted to-device event from {evt.content.sender_key}"
                       f" ({evt.sender})")
        decrypted_evt = await self._decrypt_olm_event(evt)
        if decrypted_evt.type == EventType.ROOM_KEY:
            await self._receive_room_key(decrypted_evt)

    async def _receive_room_key(self, evt: DecryptedOlmEvent) -> None:
        # TODO nio had a comment saying "handle this better"
        #      for the case where evt.Keys.Ed25519 is none?
        if evt.content.algorithm != EncryptionAlgorithm.MEGOLM_V1 or not evt.keys.ed25519:
            return
        await self._create_group_session(evt.sender_key, evt.keys.ed25519, evt.content.room_id,
                                         evt.content.session_id, evt.content.session_key)

    async def share_keys(self, current_otk_count: int) -> None:
        device_keys = (self.account.get_device_keys(self.client.mxid, self.client.device_id)
                       if not self.account.shared else None)
        one_time_keys = self.account.get_one_time_keys(self.client.mxid, self.client.device_id,
                                                       current_otk_count)
        if not device_keys and not one_time_keys:
            self.log.trace("No one-time keys nor device keys got when trying to share keys")
            return
        if device_keys:
            self.log.trace("Going to upload initial account keys")
        self.log.trace(f"Uploading {len(one_time_keys)} one-time keys")
        await self.client.upload_keys(one_time_keys=one_time_keys, device_keys=device_keys)
        self.account.shared = True
        await self.crypto_store.put_account(self.account)
        self.log.trace("Shared keys and saved account")
