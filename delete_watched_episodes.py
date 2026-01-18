#!/usr/bin/env python3
import os
from plexapi.server import PlexServer
from jellyfin_apiclient_python import JellyfinClient
from pyarr import SonarrAPI
from dotenv import load_dotenv
import datetime
import time

load_dotenv()

def add_to_log(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(os.getenv('LOG_FILE'), 'a') as f:
        f.write(f'[{timestamp}] {message}\n')

try:
    # Check and prompt for necessary environment variables
    sonarr_url = os.getenv('SONARR_URL')
    sonarr_key = os.getenv('SONARR_KEY')
    delete_by_default = os.getenv('DEFAULT_DELETE')

    # Validate and prompt for the number of days until deletion
    days_until_deletion = os.getenv('DAYS_TO_DELETE', '2')
    if days_until_deletion:
        while True:
            try:
                days_until_deletion = int(days_until_deletion)
                if days_until_deletion < 0:
                    days_until_deletion = 2
                else:
                    break
            except ValueError:
                days_until_deletion = 2
    else:
        days_until_deletion = 2

    episode_dict = {}

    media_service = os.getenv('MEDIA_SERVICE', 'Plex')
    match media_service:
        case 'plex':
            plex_url = os.getenv('PLEX_URL')
            plex_token = os.getenv('PLEX_TOKEN')

            # Add 'd' to days_until_deletion
            days_until_deletion = str(days_until_deletion) + "d"

            # Set up Plex and Sonarr instances
            plex = PlexServer(plex_url, plex_token)
            showLibrary = plex.library.section('TV Shows')
            
            #Get All Unwatched Episodes watched after days until deletion and add to an array nested dictionary in the format {Show:[Episodes]}
            for episode in showLibrary.search(unwatched=False,libtype='episode',filters={"lastViewedAt<<":days_until_deletion,"genre=" if delete_by_default == "false" else "genre!=": "Delete" if delete_by_default == "false" else "Keep"}):
                for guid in episode.season().show().guids:
                    if 'tvdb' in str(guid):
                        tvShowKey = str(guid)[13:-1]
                if tvShowKey not in episode_dict:
                    episode_dict[tvShowKey] = []
                for guid in episode.guids:
                    if 'tvdb' in str(guid):
                        episode_dict[tvShowKey].append(str(guid)[13:-1])
        
        case "jellyfin":
            jellyfin_url = os.getenv('JELLYFIN_URL')
            jellyfin_token = os.getenv('JELLYFIN_TOKEN')

            client = JellyfinClient()
            client.config.data["auth.ssl"] = True
            client.config.data["app.name"] = 'sonarr_sync_app'
            client.config.data["app.version"] = '0.0.1'
            client.authenticate({"Servers": [{"AccessToken": jellyfin_token, "address": jellyfin_url}]}, discover=False)
            
            params={
                "recursive": "true",
                "includeItemTypes": "series",
                "filters": "IsPlayed",
                "fields": "ProviderIds",
                "isFavorite": "false"
            }

            user_id = client.jellyfin._get("Users")[0]['Id']
            watched_series = client.jellyfin._get(f"Users/{user_id}/Items", params=params)['Items']
            
            params={
                "userId": user_id,
                "fields": "ProviderIds",
                "enableUserData": "true"
            }
            for series in watched_series:
                episodes = client.jellyfin._get(f"Shows/{series['Id']}/Episodes", params=params)['Items']
                tvShowKey = series['ProviderIds']['Tvdb']
                for episode in episodes:
                    userData = episode['UserData']
                
                    if userData['Played'] == True and datetime.datetime.fromisoformat(userData['LastPlayedDate'].replace("Z", "+00:00")).date() < (datetime.datetime.today() - datetime.timedelta(days=days_until_deletion)).date():
                        if tvShowKey not in episode_dict:
                            episode_dict[tvShowKey] = []
                        episode_dict[tvShowKey].append(episode['ProviderIds']['Tvdb'])

    deleted_episode = False
    
    sonarr = SonarrAPI(sonarr_url, sonarr_key)
    payload = {"monitored": False}

    #Unmonitor and Delete all old watched episodes
    for tvshow_id, episode_ids in episode_dict.items():
        sonarr_series = sonarr.get_series(id_=tvshow_id,tvdb=True)[0]
        sonarr_series_title = sonarr_series['title']
        sonarr_series_id = sonarr_series['id']
        sonarr_episodes = sonarr.get_episode(id_=sonarr_series_id,series=True)
        for episode in sonarr_episodes:
            if str(episode["tvdbId"]) in episode_ids and episode['hasFile'] == True:
                deleted_episode = True
                sonarr.upd_episode(episode['id'],payload)
                sonarr.del_episode_file(episode['episodeFileId'])
                add_to_log("Unmonitored and Deleted " + sonarr_series_title + " S" + str(episode['seasonNumber']) + "E" + str(episode['episodeNumber']))
                print("Unmonitored and Deleted " + sonarr_series_title + " S" + str(episode['seasonNumber']) + "E" + str(episode['episodeNumber']))
                # If episode is last in season then unmonitor season
                season_stats = next(i for i in sonarr_series['seasons'] if i['seasonNumber'] == episode['seasonNumber'])
                if episode['episodeNumber'] == season_stats['statistics']['totalEpisodeCount']:
                    next(i for i in sonarr_series['seasons'] if i['seasonNumber'] == episode['seasonNumber'])['monitored'] = False
                    sonarr.upd_series(sonarr_series)
                    add_to_log("Unmonitored " + sonarr_series_title + " Season " + str(episode['seasonNumber']))
                    print("Unmonitored " + sonarr_series_title + " Season " + str(episode['seasonNumber']))

    match media_service:
        case 'plex':            
            showLibrary.update()
            showLibrary.emptyTrash()
        case 'jellyfin':
            client.jellyfin.refresh_library()

    add_to_log("Deleted All Watched Episodes") if deleted_episode else add_to_log(f"No Episodes to Delete from {media_service}")
    print("Deleted All Watched Episodes") if deleted_episode else print("No Episodes to Delete")

except Exception as error:
    add_to_log("Script failed due to " + str(error))
    print("Script failed due to ", error)


should_sleep = (os.getenv("SHOULD_SLEEP", "false").lower()) == "true"
sleep_hours = int(os.getenv("SLEEP_HOURS", 24))
if should_sleep:
    time.sleep(60*60*sleep_hours)