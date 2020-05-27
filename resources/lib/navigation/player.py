# -*- coding: utf-8 -*-
"""
    Copyright (C) 2017 Sebastian Golasch (plugin.video.netflix)
    Copyright (C) 2018 Caphm (original implementation module)
    Handle playback requests

    SPDX-License-Identifier: MIT
    See LICENSES/MIT.md for more information.
"""
from __future__ import absolute_import, division, unicode_literals

import xbmcplugin
import xbmcgui

from resources.lib.api.exceptions import MetadataNotAvailable, InputStreamHelperError
from resources.lib.api.paths import EVENT_PATHS
from resources.lib.database.db_utils import TABLE_SESSION
from resources.lib.globals import g
import resources.lib.common as common
import resources.lib.api.api_requests as api
import resources.lib.kodi.infolabels as infolabels
import resources.lib.kodi.ui as ui

# Note: On SERVICE_URL_FORMAT with python 3, using 'localhost' slowdown the call (Windows OS is affected),
# so the time that Kodi takes to start a video increases, (due to requests exchange between ISA and the add-on)
# not sure if it is an urllib issue
SERVICE_URL_FORMAT = 'http://127.0.0.1:{port}'
MANIFEST_PATH_FORMAT = '/manifest?id={videoid}'
LICENSE_PATH_FORMAT = '/license?id={videoid}'

INPUTSTREAM_SERVER_CERTIFICATE = (
    'Cr0CCAMSEOVEukALwQ8307Y2+LVP+0MYh/HPkwUijgIwggEKAoIBAQDm875btoWUbGqQD8eA'
    'GuBlGY+Pxo8YF1LQR+Ex0pDONMet8EHslcZRBKNQ/09RZFTP0vrYimyYiBmk9GG+S0wB3CRI'
    'TgweNE15cD33MQYyS3zpBd4z+sCJam2+jj1ZA4uijE2dxGC+gRBRnw9WoPyw7D8RuhGSJ95O'
    'Etzg3Ho+mEsxuE5xg9LM4+Zuro/9msz2bFgJUjQUVHo5j+k4qLWu4ObugFmc9DLIAohL58UR'
    '5k0XnvizulOHbMMxdzna9lwTw/4SALadEV/CZXBmswUtBgATDKNqjXwokohncpdsWSauH6vf'
    'S6FXwizQoZJ9TdjSGC60rUB2t+aYDm74cIuxAgMBAAE6EHRlc3QubmV0ZmxpeC5jb20SgAOE'
    '0y8yWw2Win6M2/bw7+aqVuQPwzS/YG5ySYvwCGQd0Dltr3hpik98WijUODUr6PxMn1ZYXOLo'
    '3eED6xYGM7Riza8XskRdCfF8xjj7L7/THPbixyn4mULsttSmWFhexzXnSeKqQHuoKmerqu0n'
    'u39iW3pcxDV/K7E6aaSr5ID0SCi7KRcL9BCUCz1g9c43sNj46BhMCWJSm0mx1XFDcoKZWhpj'
    '5FAgU4Q4e6f+S8eX39nf6D6SJRb4ap7Znzn7preIvmS93xWjm75I6UBVQGo6pn4qWNCgLYlG'
    'GCQCUm5tg566j+/g5jvYZkTJvbiZFwtjMW5njbSRwB3W4CrKoyxw4qsJNSaZRTKAvSjTKdqV'
    'DXV/U5HK7SaBA6iJ981/aforXbd2vZlRXO/2S+Maa2mHULzsD+S5l4/YGpSt7PnkCe25F+nA'
    'ovtl/ogZgjMeEdFyd/9YMYjOS4krYmwp3yJ7m9ZzYCQ6I8RQN4x/yLlHG5RH/+WNLNUs6JAZ'
    '0fFdCmw=')


@common.inject_video_id(path_offset=0, pathitems_arg='videoid', inject_full_pathitems=True)
@common.time_execution(immediate=False)
def play(videoid):
    """Play an episode or movie as specified by the path"""
    is_upnext_enabled = g.ADDON.getSettingBool('UpNextNotifier_enabled')
    # For db settings 'upnext_play_callback_received' and 'upnext_play_callback_file_type' see action_controller.py
    is_upnext_callback_received = g.LOCAL_DB.get_value('upnext_play_callback_received', False)
    is_upnext_callback_file_type_strm = g.LOCAL_DB.get_value('upnext_play_callback_file_type', '') == 'strm'
    # This is the only way found to know if the played item come from the add-on itself or from Kodi library
    # also when Up Next Add-on is used
    is_played_from_addon = not g.IS_ADDON_EXTERNAL_CALL or (g.IS_ADDON_EXTERNAL_CALL and
                                                            is_upnext_callback_received and
                                                            not is_upnext_callback_file_type_strm)
    common.info('Playing {} from {} (Is Up Next Add-on call: {})',
                videoid,
                'add-on' if is_played_from_addon else 'external call',
                is_upnext_callback_received)

    # Profile switch when playing from Kodi library
    if not is_played_from_addon:
        if not _profile_switch():
            xbmcplugin.endOfDirectory(g.PLUGIN_HANDLE, succeeded=False)
            return

    # Get metadata of videoid
    try:
        metadata = api.get_metadata(videoid)
        common.debug('Metadata is {}', metadata)
    except MetadataNotAvailable:
        common.warn('Metadata not available for {}', videoid)
        metadata = [{}, {}]

    # Check parental control PIN
    pin_result = _verify_pin(metadata[0].get('requiresPin', False))
    if not pin_result:
        if pin_result is not None:
            ui.show_notification(common.get_local_string(30106), time=8000)
        xbmcplugin.endOfDirectory(g.PLUGIN_HANDLE, succeeded=False)
        return

    # Generate the xbmcgui.ListItem to be played
    list_item = get_inputstream_listitem(videoid)

    # STRM file resume workaround (Kodi library)
    resume_position = _strm_resume_workaroud(is_played_from_addon, videoid)
    if resume_position == '':
        xbmcplugin.setResolvedUrl(handle=g.PLUGIN_HANDLE, succeeded=False, listitem=list_item)
        return

    info_data = None
    event_data = {}
    videoid_next_episode = None

    # Get Infolabels and Arts for the videoid to be played, and for the next video if it is an episode (for UpNext)
    if not is_played_from_addon or is_upnext_enabled:
        if is_upnext_enabled and videoid.mediatype == common.VideoId.EPISODE:
            # When UpNext is enabled, get the next episode to play
            videoid_next_episode = _upnext_get_next_episode_videoid(videoid, metadata)
        info_data = infolabels.get_info_from_netflix(
            [videoid, videoid_next_episode] if videoid_next_episode else [videoid])
        info, arts = info_data[videoid.value]
        # When a item is played from Kodi library or Up Next add-on is needed set info and art to list_item
        list_item.setInfo('video', info)
        list_item.setArt(arts)

    # Get event data for videoid to be played (needed for sync of watched status with Netflix)
    if (g.ADDON.getSettingBool('ProgressManager_enabled') and
            videoid.mediatype in [common.VideoId.MOVIE, common.VideoId.EPISODE] and
            is_played_from_addon):
        # Enable the progress manager only when:
        # - It is not an add-on external call
        # - It is an external call, but the played item is not a STRM file
        # Todo:
        #  in theory to enable in Kodi library need implement the update watched status code for items of Kodi library
        #  by using JSON RPC Files.SetFileDetails https://github.com/xbmc/xbmc/pull/17202
        #  that can be used only on Kodi 19.x
        event_data = _get_event_data(videoid)
        event_data['videoid'] = videoid.to_dict()
        event_data['is_played_by_library'] = not is_played_from_addon

    if 'raspberrypi' in common.get_system_platform():
        _raspberry_disable_omxplayer()

    xbmcplugin.setResolvedUrl(handle=g.PLUGIN_HANDLE, succeeded=True, listitem=list_item)

    g.LOCAL_DB.set_value('last_videoid_played', videoid.to_dict(), table=TABLE_SESSION)

    # Start and initialize the action controller (see action_controller.py)
    common.debug('Sending initialization signal')
    common.send_signal(common.Signals.PLAYBACK_INITIATED, {
        'videoid': videoid.to_dict(),
        'videoid_next_episode': videoid_next_episode.to_dict() if videoid_next_episode else None,
        'metadata': metadata,
        'info_data': info_data,
        'is_played_from_addon': is_played_from_addon,
        'resume_position': resume_position,
        'event_data': event_data,
        'is_upnext_callback_received': is_upnext_callback_received}, non_blocking=True)


def get_inputstream_listitem(videoid):
    """Return a listitem that has all inputstream relevant properties set for playback of the given video_id"""
    service_url = SERVICE_URL_FORMAT.format(
        port=g.LOCAL_DB.get_value('msl_service_port', 8000))
    manifest_path = MANIFEST_PATH_FORMAT.format(videoid=videoid.value)
    list_item = xbmcgui.ListItem(path=service_url + manifest_path, offscreen=True)
    list_item.setContentLookup(False)
    list_item.setMimeType('application/xml+dash')
    list_item.setProperty('isFolder', 'false')
    list_item.setProperty('IsPlayable', 'true')

    try:
        import inputstreamhelper
        is_helper = inputstreamhelper.Helper('mpd', drm='widevine')
        inputstream_ready = is_helper.check_inputstream()
    except Exception as exc:  # pylint: disable=broad-except
        # Captures all types of ISH internal errors
        import traceback
        common.error(g.py2_decode(traceback.format_exc(), 'latin-1'))
        raise InputStreamHelperError(str(exc))

    if not inputstream_ready:
        raise Exception(common.get_local_string(30046))

    list_item.setProperty(
        key=is_helper.inputstream_addon + '.stream_headers',
        value='user-agent=' + common.get_user_agent())
    list_item.setProperty(
        key=is_helper.inputstream_addon + '.license_type',
        value='com.widevine.alpha')
    list_item.setProperty(
        key=is_helper.inputstream_addon + '.manifest_type',
        value='mpd')
    list_item.setProperty(
        key=is_helper.inputstream_addon + '.license_key',
        value=service_url + LICENSE_PATH_FORMAT.format(videoid=videoid.value) +
        '||b{SSM}!b{SID}|')
    list_item.setProperty(
        key=is_helper.inputstream_addon + '.server_certificate',
        value=INPUTSTREAM_SERVER_CERTIFICATE)
    list_item.setProperty(
        key='inputstreamaddon' if g.KODI_VERSION.is_major_ver('18') else 'inputstream',
        value=is_helper.inputstream_addon)
    return list_item


def _profile_switch():
    """Profile switch to play from the library"""
    # This is needed to play videos with the appropriate Netflix profile to avoid problems like:
    #   Missing audio/subtitle languages; Missing metadata; Wrong age restrictions;
    #   Video content not available; Sync with netflix watched status to wrong profile
    # Of course if the user still selects the wrong profile the problems remains,
    # but now will be caused only by the user for inappropriate use.
    is_playback_ask_profile = g.ADDON.getSettingBool('library_playback_ask_profile')
    if is_playback_ask_profile or not g.LOCAL_DB.get_value('library_playback_profile_guid'):
        selected_guid = ui.show_profiles_dialog(title_prefix=common.get_local_string(15213),
                                                preselect_guid=g.LOCAL_DB.get_active_profile_guid())
    else:
        selected_guid = g.LOCAL_DB.get_value('library_playback_profile_guid')
    if selected_guid:
        if not is_playback_ask_profile:
            # Save the selected profile guid
            g.LOCAL_DB.set_value('library_playback_profile_guid', selected_guid)
            # Save the selected profile name
            g.ADDON.setSetting('library_playback_profile', g.LOCAL_DB.get_profile_config('profileName', '???',
                                                                                         guid=selected_guid))
        # Perform the profile switch
        # The profile switch is done to NFSession, the MSL part will be switched automatically
        from resources.lib.navigation.directory_utils import activate_profile
        if not activate_profile(selected_guid):
            return False
    else:
        return False
    return True


def _verify_pin(pin_required):
    if not pin_required:
        return True
    pin = ui.ask_for_pin(common.get_local_string(30002))
    return None if not pin else api.verify_pin(pin)


def _strm_resume_workaroud(is_played_from_addon, videoid):
    """Workaround for resuming STRM files from library"""
    if is_played_from_addon or not g.ADDON.getSettingBool('ResumeManager_enabled'):
        return None
    resume_position = infolabels.get_resume_info_from_library(videoid).get('position')
    if resume_position:
        index_selected = (ui.ask_for_resume(resume_position)
                          if g.ADDON.getSettingBool('ResumeManager_dialog') else None)
        if index_selected == -1:
            # Cancel playback
            return ''
        if index_selected == 1:
            resume_position = None
    return resume_position


def _get_event_data(videoid):
    """Get data needed to send event requests to Netflix and for resume from last position"""
    is_episode = videoid.mediatype == common.VideoId.EPISODE
    req_videoids = [videoid]
    if is_episode:
        # Get also the tvshow data
        req_videoids.append(videoid.derive_parent(0))

    raw_data = api.get_video_raw_data(req_videoids, EVENT_PATHS)
    if not raw_data:
        return {}
    common.debug('Event data: {}', raw_data)
    videoid_data = raw_data['videos'][videoid.value]

    if is_episode:
        # Get inQueue from tvshow data
        is_in_mylist = raw_data['videos'][str(req_videoids[1].value)]['queue'].get('inQueue', False)
    else:
        is_in_mylist = videoid_data['queue'].get('inQueue', False)

    event_data = {'resume_position':
                  videoid_data['bookmarkPosition'] if videoid_data['bookmarkPosition'] > -1 else None,
                  'runtime': videoid_data['runtime'],
                  'request_id': videoid_data['requestId'],
                  'watched': videoid_data['watched'],
                  'is_in_mylist': is_in_mylist}
    if videoid.mediatype == common.VideoId.EPISODE:
        event_data['track_id'] = videoid_data['trackIds']['trackId_jawEpisode']
    else:
        event_data['track_id'] = videoid_data['trackIds']['trackId_jaw']
    return event_data


def _upnext_get_next_episode_videoid(videoid, metadata):
    """Determine the next episode and get the videoid"""
    try:
        videoid_next_episode = _find_next_episode(videoid, metadata)
        common.debug('Next episode is {}', videoid_next_episode)
        return videoid_next_episode
    except (TypeError, KeyError):
        # import traceback
        # common.debug(g.py2_decode(traceback.format_exc(), 'latin-1'))
        common.debug('There is no next episode, not setting up Up Next')
        return None


def _find_next_episode(videoid, metadata):
    try:
        # Find next episode in current season
        episode = common.find(metadata[0]['seq'] + 1, 'seq',
                              metadata[1]['episodes'])
        return common.VideoId(tvshowid=videoid.tvshowid,
                              seasonid=videoid.seasonid,
                              episodeid=episode['id'])
    except (IndexError, KeyError):
        # Find first episode of next season
        next_season = common.find(metadata[1]['seq'] + 1, 'seq',
                                  metadata[2]['seasons'])
        episode = common.find(1, 'seq', next_season['episodes'])
        return common.VideoId(tvshowid=videoid.tvshowid,
                              seasonid=next_season['id'],
                              episodeid=episode['id'])


def _raspberry_disable_omxplayer():
    """Check and disable OMXPlayer (not compatible with Netflix video streams)"""
    # Only Kodi 18 has this property, from Kodi 19 OMXPlayer has been removed
    if not g.KODI_VERSION.is_major_ver('18'):
        return
    value = common.json_rpc('Settings.GetSettingValue', {'setting': 'videoplayer.useomxplayer'})
    if value.get('value'):
        common.json_rpc('Settings.SetSettingValue', {'setting': 'videoplayer.useomxplayer', 'value': False})
