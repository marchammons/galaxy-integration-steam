import asyncio
import logging

import galaxy.api.errors

from protocol.protobuf_client import ProtobufClient
from protocol.consts import EResult, EFriendRelationship, EPersonaState
from friends_cache import FriendsCache
from games_cache import GamesCache
from stats_cache import StatsCache
from times_cache import TimesCache


logger = logging.getLogger(__name__)


def translate_error(result: EResult):
    assert result != EResult.OK
    data = {
        "result": result
    }
    if result in (
        EResult.AccountNotFound,
        EResult.InvalidSteamID,
        EResult.InvalidLoginAuthCode,
        EResult.AccountLogonDeniedNoMailSent,
        EResult.AccountLoginDeniedNeedTwoFactor
    ):
        return galaxy.api.errors.InvalidCredentials(data)
    if result in (
        EResult.ConnectFailed,
        EResult.IOFailure,
        EResult.RemoteDisconnect
    ):
        return galaxy.api.errors.NetworkError(data)
    if result in (
        EResult.Busy,
        EResult.ServiceUnavailable,
        EResult.Pending,
        EResult.IPNotFound,
        EResult.TryAnotherCM,
        EResult.Cancelled
    ):
        return galaxy.api.errors.BackendNotAvailable(data)
    if result == EResult.Timeout:
        return galaxy.api.errors.BackendTimeout(data)
    if result in (
        EResult.RateLimitExceeded,
        EResult.LimitExceeded,
        EResult.Suspended,
        EResult.AccountLocked,
        EResult.AccountLogonDeniedVerifiedEmailRequired
    ):
        return galaxy.api.errors.TemporaryBlocked(data)
    if result == EResult.Banned:
        return galaxy.api.errors.Banned(data)
    if result in (
        EResult.AccessDenied,
        EResult.InsufficientPrivilege,
        EResult.LogonSessionReplaced,
        EResult.Blocked,
        EResult.Ignored,
        EResult.AccountDisabled,
        EResult.AccountNotFeatured,
        EResult.AccountLogonDenied
    ):
        return galaxy.api.errors.AccessDenied(data)
    if result in (
        EResult.DataCorruption,
        EResult.DiskFull,
        EResult.RemoteCallFailed,
        EResult.RemoteFileConflict,
        EResult.BadResponse
    ):
        return galaxy.api.errors.BackendError

    return galaxy.api.errors.UnknownError(data)


class ProtocolClient:
    _STATUS_FLAG = 1106

    def __init__(self, socket, friends_cache: FriendsCache, games_cache: GamesCache, translations_cache: dict, stats_cache: StatsCache, times_cache: TimesCache):
        self._protobuf_client = ProtobufClient(socket)
        self._protobuf_client.log_on_handler = self._log_on_handler
        self._protobuf_client.log_off_handler = self._log_off_handler
        self._protobuf_client.relationship_handler = self._relationship_handler
        self._protobuf_client.user_info_handler = self._user_info_handler
        self._protobuf_client.app_info_handler = self._app_info_handler
        self._protobuf_client.license_import_handler = self._license_import_handler
        self._protobuf_client.package_info_handler = self._package_info_handler
        self._protobuf_client.translations_handler = self._translations_handler
        self._protobuf_client.stats_handler = self._stats_handler
        self._protobuf_client.times_handler = self._times_handler
        self._protobuf_client.times_import_finished_handler = self._times_import_finished_handler
        self._friends_cache = friends_cache
        self._games_cache = games_cache
        self._translations_cache = translations_cache
        self._stats_cache = stats_cache
        self._times_cache = times_cache
        self._auth_lost_handler = None
        self._login_future = None

    async def close(self):
        await self._protobuf_client.close()

    async def wait_closed(self):
        await self._protobuf_client.wait_closed()

    async def run(self):
        await self._protobuf_client.run()

    async def authenticate(self, steam_id, miniprofile_id, account_name, token, auth_lost_handler):
        loop = asyncio.get_running_loop()
        self._login_future = loop.create_future()
        await self._protobuf_client.log_on(steam_id, miniprofile_id, account_name, token)
        result = await self._login_future
        if result == EResult.OK:
            self._auth_lost_handler = auth_lost_handler
        else:
            raise translate_error(result)

    async def import_game_stats(self, game_ids):
        for game_id in game_ids:
            self._protobuf_client.job_list.append({"job_name": "import_game_stats",
                                                   "game_id": game_id})

    async def import_game_times(self):
        self._protobuf_client.job_list.append({"job_name": "import_game_times"})

    async def retrieve_collections(self):
        self._protobuf_client.job_list.append({"job_name": "import_collections"})
        await self._protobuf_client.collections['event'].wait()
        collections = self._protobuf_client.collections['collections'].copy()
        self._protobuf_client.collections['event'].clear()
        self._protobuf_client.collections['collections'] = dict()
        return collections

    async def _log_on_handler(self, result: EResult):
        assert self._login_future is not None
        self._login_future.set_result(result)

    async def _log_off_handler(self, result):
        logger.warning("Logged off, result: %d", result)
        if self._auth_lost_handler is not None:
            await self._auth_lost_handler(translate_error(result))

    async def _relationship_handler(self, incremental, friends):
        logger.info(f"Received relationships: incremental={incremental}, friends={friends}")
        initial_friends = []
        new_friends = []
        for user_id, relationship in friends.items():
            if relationship == EFriendRelationship.Friend:
                if incremental:
                    self._friends_cache.add(user_id)
                    new_friends.append(user_id)
                else:
                    initial_friends.append(user_id)
            elif relationship == EFriendRelationship.None_:
                assert incremental
                self._friends_cache.remove(user_id)

        if not incremental:
            self._friends_cache.reset(initial_friends)
            # set online state to get friends statuses
            await self._protobuf_client.set_persona_state(EPersonaState.Invisible)
            await self._protobuf_client.get_friends_statuses()
            await self._protobuf_client.get_user_infos(initial_friends, self._STATUS_FLAG)

        if new_friends:
            await self._protobuf_client.get_friends_statuses()
            await self._protobuf_client.get_user_infos(new_friends, self._STATUS_FLAG)

    async def _user_info_handler(self, user_id, user_info):
        logger.info(f"Received user info: user_id={user_id}, user_info={user_info}")
        self._friends_cache.update(user_id, user_info)

    async def _license_import_handler(self, licenses):
        logger.info(f"Starting license import for {[license.package_id for license in licenses]}")
        package_ids = []

        for license in licenses:
            package_ids.append(int(license.package_id))

        self._games_cache.start_packages_import(package_ids)

        await self._protobuf_client.get_packages_info(package_ids)

    async def _app_info_handler(self, appid, title=None, game=None):
        self._games_cache.update(appid, title, game)

    async def _package_info_handler(self, package_id):
        self._games_cache.update_packages(package_id)

    async def _translations_handler(self, appid, translations=None):
        if appid and translations:
            self._translations_cache[appid] = translations[0]
        elif appid not in self._translations_cache:
            await self._protobuf_client.get_presence_localization(appid)

    async def _stats_handler(self, game_id, stats, achievements):
        self._stats_cache.update_stats(str(game_id), stats, achievements)

    async def _times_handler(self, game_id, time_played, last_played):
        self._times_cache.update_time(str(game_id), time_played, last_played)

    async def _times_import_finished_handler(self, finished):
        self._times_cache.times_import_finished(finished)
