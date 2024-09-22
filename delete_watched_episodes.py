#!/usr/bin/env python3
import os
from plexapi.server import PlexServer
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
    plex_url = os.getenv('PLEX_URL')
    plex_token = os.getenv('PLEX_TOKEN')
    sonarr_url = os.getenv('SONARR_URL')
    sonarr_key = os.getenv('SONARR_KEY')
    delete_by_default = os.getenv('DEFAULT_DELETE')

    # Validate and prompt for the number of days until deletion
    days_until_deletion = os.getenv('DAYS_TO_DELETE')
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

    # Add 'd' to days_until_deletion
    days_until_deletion = str(days_until_deletion) + "d"

    # Set up Plex and Sonarr instances
    plex = PlexServer(plex_url, plex_token)
    showLibrary = plex.library.section('TV Shows')
    sonarr = SonarrAPI(sonarr_url, sonarr_key)
    payload = {"monitored": False}

    episode_dict = {}
    #Get All Unwatched Episodes watched after days until deletion and add to an array nested dictionary in the format {Show:[Episodes]}
    for episode in showLibrary.search(unwatched=False,libtype='episode',filters={"lastViewedAt<<":days_until_deletion,"genre=" if delete_by_default == "false" else "genre!=": "Delete" if delete_by_default == "false" else "Keep"}):
        for guid in episode.season().show().guids:
            if 'tvdb' in str(guid):
                tvShowKey = str(guid)[13:-1]
        if tvShowKey not in episode_dict:
            episode_dict[tvShowKey] = []
        for guid in episode.guids:
            if 'tvdb' in str(guid):
                episode_dict[tvShowKey].append(int(str(guid)[13:-1]))

    deleted_episode = False

    #Unmonitor and Delete all old watched episodes
    for tvshow_id, episode_ids in episode_dict.items():
        sonarr_series = sonarr.get_series(id_=tvshow_id,tvdb=True)[0]
        sonarr_series_title = sonarr_series['title']
        sonarr_series_id = sonarr_series['id']
        sonarr_episodes = sonarr.get_episode(id_=sonarr_series_id,series=True)
        for episode in sonarr_episodes:
            if episode["tvdbId"] in episode_ids and episode['hasFile'] == True:
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
                
    showLibrary.update()
    showLibrary.emptyTrash()
    add_to_log("Deleted All Watched Episodes") if deleted_episode else add_to_log("No Episodes to Delete")
    print("Deleted All Watched Episodes") if deleted_episode else print("No Episodes to Delete")

except Exception as error:
    add_to_log("Script failed due to " + str(error))
    print("Script failed due to ", error)

time.sleep(60*60*24)